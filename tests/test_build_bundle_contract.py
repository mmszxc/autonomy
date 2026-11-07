from pathlib import Path


def test_bundle_keeps_dependency_trees_and_manifests_archive_payload():
    script = (Path(__file__).parents[1] / "tools" / "build_bundle.sh").read_text(encoding="utf-8")

    assert "--exclude='_vendor'" not in script
    assert "--exclude='_vendor_ml'" not in script
    assert "--exclude='seed_src/hermes/model'" not in script
    assert "--exclude='*.db'" not in script
    assert "--exclude='*.log'" not in script
    assert "tar -xzf \"$OUT\" -C \"$STAGING\"" in script
    assert 'cd "$STAGING"' in script
    assert 'MANIFEST="${OUT}.manifest.sha256"' in script
    assert 'VENDOR_ROOT="$STAGING/plugins/autonomy/_vendor"' in script
    assert 'VENDOR_FILES' in script
    assert 'ARCHIVE_FILES' in script and 'MANIFEST_FILES' in script
