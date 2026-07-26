import json
import logging
import re
from sys import exit
from pathlib import Path
from os import getenv
import subprocess
from src import (
    r2,
    utils,
    release,
    downloader
)

def _should_retry_with_older_version(output: str | None) -> bool:
    if not output:
        return False
    t = output.lower()
    return (
        "failed to match the fingerprint" in t
        or "patch.patchexception" in t
        or ("fingerprint" in t and "failed" in t)
        or "patching aborted" in t
    )

def run_build(app_name: str, source: str, arch: str = "universal") -> list[str]:
    download_files, name = downloader.download_required(source)

    logging.info(f"📦 Downloaded {len(download_files)} files for {source}:")
    for file in download_files:
        logging.info(f"  - {file.name} ({file.stat().st_size} bytes)")

    is_morphe = False
    is_revanced = False

    for file in download_files:
        if "morphe-desktop" in file.name.lower():
            is_morphe = True
            break
        elif "revanced-cli" in file.name.lower():
            is_revanced = True
            break

    if not is_morphe and not is_revanced:
        for file in download_files:
            if file.suffix == ".mpp":
                is_morphe = True
                break
            elif file.suffix in [".rvp", ".jar"] and "patches" in file.name.lower():
                is_revanced = True
                break

    if not is_morphe and not is_revanced:
        is_morphe = "morphe" in source.lower() or "custom" in source.lower()
        is_revanced = not is_morphe

    logging.info(f"🔍 Detected: {'Morphe' if is_morphe else 'ReVanced'} source type")

    if is_morphe:
        cli = utils.find_file(download_files, contains="morphe-desktop", suffix=".jar", exclude=["dev"])
        if not cli:
            cli = utils.find_file(download_files, contains="morphe", suffix=".jar")
        
        patches = utils.find_file(download_files, contains="patches", suffix=".mpp")
        if not patches:
            patches = utils.find_file(download_files, suffix=".mpp")
    else:
        cli = utils.find_file(download_files, contains="revanced-cli", suffix=".jar")
        patches = utils.find_file(download_files, contains="patches", suffix=".rvp")
        
        if not patches:
            patches = utils.find_file(download_files, contains="patches", suffix=".jar")

    if not cli:
        logging.error(f"❌ CLI not found for source: {source}")
        return []
    if not patches:
        logging.error(f"❌ Patches not found for source: {source}")
        return []

    logging.info(f"✅ Using CLI: {cli.name}")
    logging.info(f"✅ Using patches: {patches.name}")

    download_methods = [
        downloader.download_apkmirror,
        downloader.download_apkpure,
        downloader.download_uptodown,
        downloader.download_aptoide
    ]

    input_apk = None
    version = None
    candidates: list[str] = []
    used_method = None
    
    for method in download_methods:
        input_apk, version, candidates = method(app_name, str(cli), str(patches), arch)
        if input_apk:
            used_method = method
            break

    if input_apk is None or not used_method or not version:
        logging.error(f"❌ Failed to download APK for {app_name}")
        return []

    versions_to_try: list[str] = [version]
    if candidates and version in candidates:
        versions_to_try += [v for v in candidates if v != version]

    base_excludes = []
    base_includes = []
    patches_path = Path("patches") / f"{app_name}-{source}.txt"
    if patches_path.exists():
        with patches_path.open('r') as patches_file:
            for line in patches_file:
                line = line.strip()
                if line.startswith('-'):
                    if "Change package name" not in line:
                        base_excludes.extend(["-d", line[1:].strip()])
                elif line.startswith('+'):
                    if "Change package name" not in line:
                        base_includes.extend(["-e", line[1:].strip()])

    for attempt_idx, ver in enumerate(versions_to_try):
        if attempt_idx > 0:
            logging.warning(f"Retrying {app_name}/{source}/{arch} with older version {ver} due to patch failure...")
            try:
                input_apk.unlink(missing_ok=True)
            except Exception:
                pass

            input_apk, version, _ = used_method(app_name, str(cli), str(patches), arch, override_version=ver)
            if input_apk is None:
                continue
            version = ver

        if input_apk.suffix != ".apk":
            logging.warning("Input file is not .apk, using APKEditor to merge")
            apk_editor = downloader.download_apkeditor()
            merged_apk = input_apk.with_suffix(".apk")

            utils.run_process([
                "java", "-jar", apk_editor, "m",
                "-i", str(input_apk),
                "-o", str(merged_apk)
            ], silent=True)

            input_apk.unlink(missing_ok=True)

            if not merged_apk.exists():
                raise RuntimeError("Merged APK file not found")

            clean_name = re.sub(r'\(\d+\)', '', merged_apk.name)
            clean_name = re.sub(r'-\d+_', '_', clean_name)
            if clean_name != merged_apk.name:
                clean_apk = merged_apk.with_name(clean_name)
                merged_apk.rename(clean_apk)
                merged_apk = clean_apk

            input_apk = merged_apk

        if arch != "universal":
            if arch == "arm64-v8a":
                utils.run_process(["zip", "--delete", str(input_apk), "lib/x86/*", "lib/x86_64/*", "lib/armeabi-v7a/*"], silent=True, check=False)
            elif arch == "armeabi-v7a":
                utils.run_process(["zip", "--delete", str(input_apk), "lib/x86/*", "lib/x86_64/*", "lib/arm64-v8a/*"], silent=True, check=False)
        else:
            utils.run_process(["zip", "--delete", str(input_apk), "lib/x86/*", "lib/x86_64/*"], silent=True, check=False)

        try:
            fixed_apk = Path(f"{app_name}-fixed-v{version}.apk")
            subprocess.run(["zip", "-FF", str(input_apk), "--out", str(fixed_apk)], check=False, capture_output=True)

            if fixed_apk.exists() and fixed_apk.stat().st_size > 0:
                input_apk.unlink(missing_ok=True)
                fixed_apk.rename(input_apk)
        except Exception as e:
            pass

        try:
            generated_apks = []
            variants = [
                {"label": "unCLONE", "args": ["-d", "Change package name"]},
                {"label": "CLONE", "args": ["-e", "Change package name", "-O", "packageName=morphe.google.photos"]}
            ]

            for variant in variants:
                logging.info(f"🔨 Generating {variant['label'].upper()} variant...")
                output_apk = Path(f"{app_name}-{arch}-{variant['label']}-patch-v{version}.apk")
                variant_flags = base_excludes + base_includes + variant['args']

                if is_morphe:
                    try:
                        morphe_cmd = [
                            "java", "-jar", str(cli),
                            "patch", "--patches", str(patches),
                            "--out", str(output_apk), str(input_apk),
                            "--continue-on-error",
                            *variant_flags
                        ]
                        utils.run_process(morphe_cmd, capture=True, stream=True)
                    except subprocess.CalledProcessError:
                        morphe_cmd = [
                            "java", "-jar", str(cli),
                            "patch", "--patches", str(patches),
                            "--input", str(input_apk),
                            "--output", str(output_apk),
                            "--continue-on-error",
                            *variant_flags
                        ]
                        utils.run_process(morphe_cmd, capture=True, stream=True)
                else:
                    cli_name = Path(cli).name.lower()
                    is_revanced_v6_or_newer = ('revanced-cli-6' in cli_name or 'revanced-cli-7' in cli_name or 'revanced-cli-8' in cli_name)

                    if is_revanced_v6_or_newer:
                        utils.run_process([
                            "java", "-jar", str(cli),
                            "patch", "-p", str(patches), "-b",
                            "--out", str(output_apk), str(input_apk),
                            "--continue-on-error",
                            *variant_flags
                        ], capture=True, stream=True)
                    else:
                        utils.run_process([
                            "java", "-jar", str(cli),
                            "patch", "--patches", str(patches),
                            "--out", str(output_apk), str(input_apk),
                            "--continue-on-error",
                            *variant_flags
                        ], capture=True, stream=True)

                patchver = release.extract_version(str(patches))
                output_custom_name = f"{app_name.lower()}-{source.lower()}-{variant['label']}_{version}-v{patchver}.apk"
                signed_apk = Path(output_custom_name)

                apksigner = utils.find_apksigner()
                if not apksigner:
                    raise RuntimeError("apksigner not found")

                ks_path = getenv("KS_PATH", "/tmp/custom.keystore")
                ks_pass = getenv("KEYSTORE_PASSWORD", "")
                ks_alias = getenv("KEY_ALIAS", "")

                try:
                    utils.run_process([
                        str(apksigner), "sign", "--verbose",
                        "--ks", str(ks_path),
                        "--ks-type", "JKS",
                        "--ks-pass", f"pass:{ks_pass}",
                        "--key-pass", f"pass:{ks_pass}",
                        "--ks-key-alias", str(ks_alias),
                        "--in", str(output_apk), "--out", str(signed_apk)
                    ], capture=True, stream=True)
                except Exception:
                    utils.run_process([
                        str(apksigner), "sign", "--verbose",
                        "--min-sdk-version", "21",
                        "--ks", str(ks_path),
                        "--ks-type", "JKS",
                        "--ks-pass", f"pass:{ks_pass}",
                        "--key-pass", f"pass:{ks_pass}",
                        "--ks-key-alias", str(ks_alias),
                        "--in", str(output_apk), "--out", str(signed_apk)
                    ], capture=True, stream=True)

                output_apk.unlink(missing_ok=True)
                print(f"✅ APK built: {signed_apk.name}")
                generated_apks.append(str(signed_apk))

            input_apk.unlink(missing_ok=True)
            return generated_apks

        except subprocess.CalledProcessError as e:
            input_apk.unlink(missing_ok=True)
            if attempt_idx < len(versions_to_try) - 1 and _should_retry_with_older_version(getattr(e, "output", None)):
                continue
            raise

    return []

def main():
    app_name = getenv("APP_NAME")
    source = getenv("SOURCE")

    if not app_name or not source:
        logging.error("APP_NAME and SOURCE environment variables must be set")
        exit(1)

    arch_config_path = Path("arch-config.json")
    if arch_config_path.exists():
        with open(arch_config_path) as f:
            arch_config = json.load(f)
        
        arches = ["universal"]
        for config in arch_config:
            if config["app_name"] == app_name and config["source"] == source:
                arches = config["arches"]
                break
        
        root_files = list(Path(".").glob("*"))
        detected_patches = utils.find_file(root_files, contains="patches", suffix=".mpp") or utils.find_file(root_files, suffix=".mpp")
        detected_cli = utils.find_file(root_files, contains="morphe-desktop", suffix=".jar") or utils.find_file(root_files, suffix=".jar")

        patches_str = str(detected_patches) if detected_patches else "patches"
        cli_str = str(detected_cli) if detected_cli else "cli"

        built_apks = []
        for arch in arches:
            logging.info(f"🔨 Building {app_name} for {arch} architecture...")
            apk_paths = run_build(app_name, source, arch)
            
            if apk_paths:
                for path in apk_paths:
                    built_apks.append(path)
                    print(f"✅ Validated target: {Path(path).name}")
                    release.create_github_release(app_name, patches_str, cli_str, path)
        
        print(f"\n🎯 Built {len(built_apks)} APK(s) for {app_name}:")
        for apk in built_apks:
            print(f"  📱 {Path(apk).name}")
        
    else:
        logging.warning("arch-config.json not found, building universal only")
        apk_paths = run_build(app_name, source, "universal")
        if apk_paths:
            for path in apk_paths:
                print(f"🎯 Final APK path: {path}")

if __name__ == "__main__":
    main()
