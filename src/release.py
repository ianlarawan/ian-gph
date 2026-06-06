import re
import json
from sys import exit
from pathlib import Path
from src import repository, gh

def convert_title(text):
    if not text or not isinstance(text, str):
        return text
    if text.lower() == 'gphotos':
        return 'GPH'
    return text.title()

def extract_version(file_path):
    if not file_path:
        return 'unknown'
    path = Path(file_path)
    base_name = path.stem
    
    # Matches version numbers cleanly from the middle segments of your custom output template array
    # e.g., morphe-gphotos_CLONE_7.76.0_arm64-v8a_1.0.4
    match = re.search(r'_(?:CLONE|unCLONE)_(\d+\.\d+(?:\.\d+)?(-[a-z]+\.\d+)?)', base_name)
    if match:
        return match.group(1)
        
    match = re.search(r'(\d+\.\d+\.\d+|\d{3,})', base_name)
    return match.group(1) if match else 'unknown'

def create_github_release(name, patches_name, cli_name, apk_file_paths):
    """Creates a unified repository release slot containing all variants"""
    if isinstance(apk_file_paths, str):
        apk_file_paths = [apk_file_paths]

    if not apk_file_paths:
        return

    patches_dir = Path(".")
    mpp_files = list(patches_dir.glob("*.mpp"))
    
    if mpp_files:
        patchver = re.search(r'(\d+\.\d+\.\d+(-[a-z]+\.\d+)?(-release\d*)?)', mpp_files[0].stem)
        patchver = patchver.group(1) if patchver else 'unknown'
    else:
        patchver = re.search(r'(\d+\.\d+\.\d+(-[a-z]+\.\d+)?(-release\d*)?)', Path(patches_name).stem)
        patchver = patchver.group(1) if patchver else 'unknown'
    
    cli_match = re.search(r'(\d+\.\d+\.\d+(-[a-z]+\.\d+)?(-release\d*)?)', Path(cli_name).stem)
    cliver = cli_match.group(1) if cli_match else patchver
    
    # Process app version format targeting the custom major.minor layout
    raw_app_version = extract_version(str(apk_file_paths[0]))
    short_version_match = re.match(r'^(\d+\.\d+)', raw_app_version)
    app_version = short_version_match.group(1) if short_version_match else raw_app_version
    
    # --- ENFORCE DYNAMIC TAG FORMAT RULES ---
    # Produces precisely: gph-7.76-1.0.4
    tag_name = f"gph-{app_version}-{patchver}"

    repo = gh.get_repo(repository)

    try:
        existing_release = repo.get_release(tag_name)
    except:
        existing_release = None

    # Clear old duplicate asset items inside the slot if updating
    if existing_release:
        for path_str in apk_file_paths:
            p = Path(path_str)
            for asset in existing_release.get_assets():
                if asset.name == p.name:
                    asset.delete_asset()

    # Drop older historical release segments tracking the same gph- tag root
    releases = list(repo.get_releases())
    for r in releases:
        if r.tag_name.startswith("gph-") and r.tag_name != tag_name:
            try:
                old_patchver = r.tag_name.split('-')[-1]
                if old_patchver < patchver:
                    r.delete_release()
            except:
                pass

    if not existing_release:
        # Structured documentation for De-ReVanced deployment layout
        release_body = f"""# Release Notes

## Build Tools:
- **De-ReVanced Patches:** v{patchver}
- **Morphe CLI:** v{cliver}

## Note:
This release includes both **CLONE** (Separated package name) and **unCLONE** (Original package overlay) application packages. 
"""
        # Formats the title string precisely to: GPH 7.76-1.0.4
        release_name = f"{convert_title(name)} {app_version}-{patchver}"
        
        existing_release = repo.create_git_release(
            tag=tag_name,
            name=release_name,
            message=release_body,
            draft=False,
            prerelease=False
        )

    # Multi-upload sequence handling loop
    for path_str in apk_file_paths:
        apk_path = Path(path_str)
        if apk_path.exists():
            existing_release.upload_asset(
                path=str(apk_path),
                label=apk_path.name,
                content_type='application/vnd.android.package-archive'
            )