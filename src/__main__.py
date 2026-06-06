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

def run_build(app_name: str, source: str, arch: str, variant: str) -> str:
    """Build APK for a specific architecture and variant (CLONE / unCLONE)"""
    download_files, name = downloader.download_required(source)

    is_morphe = False
    is_revanced = False

    for file in download_files:
        if "morphe-cli" in file.name.lower():
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

    if is_morphe:
        cli = utils.find_file(download_files, contains="morphe-cli", suffix=".jar", exclude=["dev"])
        if not cli:
            cli = utils.find_file(download_files, contains="morphe", suffix=".jar")
        patches = utils.find_file(download_files, contains="patches", suffix=".mpp")
        if not patches:
            patches = utils.find_file(download_files, suffix=".mpp")
    else:
        cli = utils.find_file(download_files, contains="revanced-cli", suffix=".jar")
        patches = utils.find_file(download_files, contains="patches", suffix=".rvp")

    if not cli or not patches:
        logging.error(f"❌ Core compilation tools missing for source {source}")
        return None

    download_methods = [
        downloader.download_apkmirror,
        downloader.download_apkpure,
        downloader.download_uptodown,
        downloader.download_aptoide
    ]

    input_apk = None
    version = None
    candidates = []
    used_method = None
    for method in download_methods:
        input_apk, version, candidates = method(app_name, str(cli), str(patches), arch)
        if input_apk:
            used_method = method
            break

    if input_apk is None or not used_method or not version:
        logging.error(f"❌ Failed to acquire source APK for {app_name}")
        return None

    versions_to_try = [version]
    if candidates and version in candidates:
        versions_to_try += [v for v in candidates if v != version]

    exclude_patches = []
    include_patches = []

    patches_path = Path("patches") / f"{app_name}-{source}.txt"
    if patches_path.exists():
        with patches_path.open('r') as patches_file:
            for line in patches_file:
                line = line.strip()
                if line.startswith('-'):
                    exclude_patches.extend(["-d", line[1:].strip()])
                elif line.startswith('+'):
                    patch_name = line[1:].strip()
                    # Skip "Change package name" global loading so variants handle it independently
                    if patch_name.lower() == "change package name":
                        continue
                    include_patches.extend(["-e", patch_name])

    # --- VARIANT-SPECIFIC PATCH CONFIGURATION ---
    if variant == "CLONE":
        # Force add it strictly for the CLONE variant along with the required options options
        include_patches.extend(["-e", "Change package name", "-O", "packageName=morphe.google.photos"])
    elif variant == "unCLONE":
        # Explicitly force disable it for the unCLONE variant to prevent leaks
        exclude_patches.extend(["-d", "Change package name"])

    for attempt_idx, ver in enumerate(versions_to_try):
        if attempt_idx > 0:
            try:
                input_apk.unlink(missing_ok=True)
            except Exception:
                pass
            input_apk, version, _ = used_method(app_name, str(cli), str(patches), arch, override_version=ver)
            if input_apk is None:
                continue
            version = ver

        if input_apk.suffix != ".apk":
            apk_editor = downloader.download_apkeditor()
            merged_apk = input_apk.with_suffix(".apk")
            utils.run_process(["java", "-jar", apk_editor, "m", "-i", str(input_apk), "-o", str(merged_apk)], silent=True)
            input_apk.unlink(missing_ok=True)
            
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
            logging.warning(f"Could not fix APK: {e}")

        output_apk = Path(f"{app_name}-{arch}-{variant}-patch-v{version}.apk")

        try:
            if is_morphe:
                try:
                    morphe_cmd = [
                        "java", "-jar", str(cli), "patch", "--patches", str(patches),
                        "--out", str(output_apk), str(input_apk),
                        *exclude_patches, *include_patches
                    ]
                    utils.run_process(morphe_cmd, capture=True, stream=True)
                except subprocess.CalledProcessError:
                    morphe_cmd = [
                        "java", "-jar", str(cli),
                        "patch",
                        "--patches", str(patches),
                        "--input", str(input_apk),
                        "--output", str(output_apk)
                    ]
                    utils.run_process(morphe_cmd, capture=True, stream=True)
            else:
                cli_name = Path(cli).name.lower()
                is_revanced_v6_or_newer = any(k in cli_name for k in ['revanced-cli-6', 'revanced-cli-7', 'revanced-cli-8'])
                if is_revanced_v6_or_newer:
                    utils.run_process(["java", "-jar", str(cli), "patch", "-p", str(patches), "-b", "--out", str(output_apk), str(input_apk), *exclude_patches, *include_patches], capture=True, stream=True)
                else:
                    utils.run_process(["java", "-jar", str(cli), "patch", "--patches", str(patches), "--out", str(output_apk), str(input_apk), *exclude_patches, *include_patches], capture=True, stream=True)
        except subprocess.CalledProcessError as e:
            input_apk.unlink(missing_ok=True)
            output_apk.unlink(missing_ok=True)
            if attempt_idx < len(versions_to_try) - 1 and _should_retry_with_older_version(getattr(e, "output", None)):
                continue
            raise

        input_apk.unlink(missing_ok=True)
        patchver = release.extract_version(str(patches))
        
        clean_version_match = re.match(r'^(\d+\.\d+\.\d+)', version)
        display_version = clean_version_match.group(1) if clean_version_match else version
        
        output_custom_name = f"morphe-gphotos_{variant}_{display_version}_{arch}_{patchver}.apk"
        signed_apk = Path(output_custom_name)

        apksigner = utils.find_apksigner()
        if not apksigner:
            raise RuntimeError("apksigner not found")

        ks_path = getenv("KS_PATH", "/tmp/custom.keystore")
        ks_pass = getenv("KEYSTORE_PASSWORD", "")
        ks_alias = getenv("KEY_ALIAS", "")

        try:
            utils.run_process([str(apksigner), "sign", "--verbose", "--ks", str(ks_path), "--ks-pass", f"pass:{ks_pass}", "--key-pass", f"pass:{ks_pass}", "--ks-key-alias", str(ks_alias), "--in", str(output_apk), "--out", str(signed_apk)], capture=True, stream=True)
        except Exception:
            utils.run_process([str(apksigner), "sign", "--verbose", "--min-sdk-version", "21", "--ks", str(ks_path), "--ks-pass", f"pass:{ks_pass}", "--key-pass", f"pass:{ks_pass}", "--ks-key-alias", str(ks_alias), "--in", str(output_apk), "--out", str(signed_apk)], capture=True, stream=True)

        output_apk.unlink(missing_ok=True)
        print(f"✅ APK built: {signed_apk.name}")
        return str(signed_apk)

    return None

def main():
    app_name = getenv("APP_NAME")
    source = getenv("SOURCE")

    if not app_name or not source:
        logging.error("APP_NAME and SOURCE environment variables must be set")
        exit(1)

    arch_config_path = Path("arch-config.json")
    arches = ["universal"]
    if arch_config_path.exists():
        with open(arch_config_path) as f:
            arch_config = json.load(f)
        for config in arch_config:
            if config["app_name"] == app_name and config["source"] == source:
                arches = config["arches"]
                break

    variants = ["CLONE", "unCLONE"]
    built_apks = []

    for arch in arches:
        for variant in variants:
            logging.info(f"🔨 Building {app_name} ({variant}) for {arch} architecture...")
            apk_path = run_build(app_name, source, arch, variant)
            if apk_path:
                built_apks.append(apk_path)
                print(f"✅ Built {variant} variant: {Path(apk_path).name}")

    if built_apks:
        release.create_github_release(app_name, "patches", "cli", built_apks)

    print(f"\n🎯 Pipeline run finished. Built {len(built_apks)} total APK variants.")

if __name__ == "__main__":
    main()