"""
Script: tests/test_configure_signing_policy.py
What: Tests for the in-image signing-policy helper.
Doing: Loads the helper from its tracked script path, writes policy/discovery files into a temporary directory, and verifies the resulting content.
Why: The native image build now calls a pure Python helper instead of a shell-plus-inline-Python script.
Goal: Keep repository trust policy generation readable, deterministic, and testable.
"""

from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPT_PATH = (
    Path(__file__).resolve().parent.parent
    / "files"
    / "scripts"
    / "configure_signing_policy.py"
)


def load_module():
    spec = importlib.util.spec_from_file_location("configure_signing_policy", SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ConfigureSigningPolicyTests(unittest.TestCase):
    def test_main_writes_policy_and_registry_discovery_files(self) -> None:
        module = load_module()

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            policy_file = temp_root / "policy.json"
            registries_dir = temp_root / "registries.d"
            key_path = temp_root / "keys" / "zfs-kinoite-complex.pub"
            key_path.parent.mkdir(parents=True, exist_ok=True)
            key_path.write_text("public-key", encoding="utf-8")

            with patch.dict(
                os.environ,
                {
                    "IMAGE_REPO": "ghcr.io/example/zfs-kinoite-complex",
                    "SIGNING_KEY_FILENAME": "zfs-kinoite-complex.pub",
                    "POLICY_FILE": str(policy_file),
                    "REGISTRIES_DIR": str(registries_dir),
                    "KEY_PATH": str(key_path),
                },
                clear=False,
            ):
                module.main()

            policy_data = json.loads(policy_file.read_text(encoding="utf-8"))
            self.assertEqual(
                policy_data["transports"]["docker"]["ghcr.io/example/zfs-kinoite-complex"][0]["keyPath"],
                str(key_path),
            )

            registry_file = registries_dir / "ghcr.io-example-zfs-kinoite-complex.yaml"
            registry_text = registry_file.read_text(encoding="utf-8")
            self.assertIn("ghcr.io/example/zfs-kinoite-complex", registry_text)
            self.assertIn("use-sigstore-attachments: true", registry_text)

    def test_main_merges_into_an_existing_policy_document(self) -> None:
        """
        The image build runs against the base image's own `/etc/containers/policy.json`,
        which already exists. Everything that file already allowed has to survive, and a
        stale rule for this repository has to be replaced rather than appended to.
        """

        module = load_module()

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            policy_file = temp_root / "policy.json"
            registries_dir = temp_root / "registries.d"
            key_path = temp_root / "keys" / "zfs-kinoite-complex.pub"
            key_path.parent.mkdir(parents=True, exist_ok=True)
            key_path.write_text("public-key", encoding="utf-8")

            policy_file.write_text(
                json.dumps(
                    {
                        "default": [{"type": "reject"}],
                        "transports": {
                            "docker": {
                                "quay.io/other/image": [{"type": "insecureAcceptAnything"}],
                                "ghcr.io/example/zfs-kinoite-complex": [
                                    {"type": "insecureAcceptAnything"}
                                ],
                            },
                            "docker-daemon": {"": [{"type": "insecureAcceptAnything"}]},
                        },
                    }
                ),
                encoding="utf-8",
            )

            with patch.dict(
                os.environ,
                {
                    "IMAGE_REPO": "ghcr.io/example/zfs-kinoite-complex",
                    "SIGNING_KEY_FILENAME": "zfs-kinoite-complex.pub",
                    "POLICY_FILE": str(policy_file),
                    "REGISTRIES_DIR": str(registries_dir),
                    "KEY_PATH": str(key_path),
                },
                clear=False,
            ):
                module.main()

            policy_data = json.loads(policy_file.read_text(encoding="utf-8"))

            self.assertEqual(policy_data["default"], [{"type": "reject"}])
            self.assertEqual(
                policy_data["transports"]["docker-daemon"],
                {"": [{"type": "insecureAcceptAnything"}]},
            )
            self.assertEqual(
                policy_data["transports"]["docker"]["quay.io/other/image"],
                [{"type": "insecureAcceptAnything"}],
            )
            self.assertEqual(
                policy_data["transports"]["docker"]["ghcr.io/example/zfs-kinoite-complex"],
                [
                    {
                        "type": "sigstoreSigned",
                        "keyPath": str(key_path),
                        "signedIdentity": {"type": "matchRepository"},
                    }
                ],
            )

    def test_main_writes_the_policy_when_the_public_key_is_not_present_yet(self) -> None:
        """
        The key is copied in by a separate build step. Ordering must not matter: the
        policy still has to be written, and the missing key must not raise.
        """

        module = load_module()

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            policy_file = temp_root / "policy.json"
            registries_dir = temp_root / "registries.d"
            key_path = temp_root / "keys" / "zfs-kinoite-complex.pub"

            with patch.dict(
                os.environ,
                {
                    "IMAGE_REPO": "ghcr.io/example/zfs-kinoite-complex",
                    "SIGNING_KEY_FILENAME": "zfs-kinoite-complex.pub",
                    "POLICY_FILE": str(policy_file),
                    "REGISTRIES_DIR": str(registries_dir),
                    "KEY_PATH": str(key_path),
                },
                clear=False,
            ):
                module.main()

            self.assertFalse(key_path.exists())
            policy_data = json.loads(policy_file.read_text(encoding="utf-8"))
            self.assertEqual(
                policy_data["transports"]["docker"]["ghcr.io/example/zfs-kinoite-complex"][0][
                    "keyPath"
                ],
                str(key_path),
            )

    def test_key_path_defaults_to_the_image_pki_directory(self) -> None:
        """
        `build-image.sh` passes only `IMAGE_REPO` and `SIGNING_KEY_FILENAME`, so the
        `keyPath` written into the policy comes from this default. If it is wrong,
        `bootc upgrade` cannot verify a signature on a booted machine.
        """

        module = load_module()

        with patch.dict(os.environ, {}, clear=True):
            resolved = module.key_path_from_env(
                signing_key_filename="zfs-kinoite-complex.pub"
            )

        self.assertEqual(resolved, Path("/etc/pki/containers/zfs-kinoite-complex.pub"))
        self.assertEqual(module.DEFAULT_KEYS_DIR, Path("/etc/pki/containers"))

    def test_key_path_override_is_ignored_when_blank(self) -> None:
        module = load_module()

        with patch.dict(os.environ, {"KEY_PATH": "   "}, clear=True):
            resolved = module.key_path_from_env(
                signing_key_filename="zfs-kinoite-complex.pub"
            )

        self.assertEqual(resolved, Path("/etc/pki/containers/zfs-kinoite-complex.pub"))

    def test_required_env_rejects_unset_and_blank_values(self) -> None:
        """
        A missing `IMAGE_REPO` must stop the build. Falling through would write a
        policy document with a rule keyed on an empty repository string.
        """

        module = load_module()

        with patch.dict(os.environ, {}, clear=True), self.assertRaises(SystemExit) as unset:
            module.required_env("IMAGE_REPO")

        self.assertIn("IMAGE_REPO", str(unset.exception))

        with (
            patch.dict(os.environ, {"SIGNING_KEY_FILENAME": "  \t "}, clear=True),
            self.assertRaises(SystemExit) as blank,
        ):
            module.required_env("SIGNING_KEY_FILENAME")

        self.assertIn("SIGNING_KEY_FILENAME", str(blank.exception))

    def test_required_env_strips_surrounding_whitespace(self) -> None:
        module = load_module()

        with patch.dict(os.environ, {"IMAGE_REPO": "  ghcr.io/example/img \n"}, clear=True):
            self.assertEqual(module.required_env("IMAGE_REPO"), "ghcr.io/example/img")

    def test_registry_file_path_uses_full_repo_path(self) -> None:
        module = load_module()
        registries_dir = Path("/tmp/registries.d")

        first = module.registry_file_path(
            image_repo="ghcr.io/danathar/zfs-kinoite-complex",
            registries_dir=registries_dir,
        )
        second = module.registry_file_path(
            image_repo="ghcr.io/other/zfs-kinoite-complex",
            registries_dir=registries_dir,
        )

        self.assertEqual(first.name, "ghcr.io-danathar-zfs-kinoite-complex.yaml")
        self.assertEqual(second.name, "ghcr.io-other-zfs-kinoite-complex.yaml")
        self.assertNotEqual(first, second)

    def test_registry_file_path_preserves_dots_and_hyphens(self) -> None:
        module = load_module()
        registry_file = module.registry_file_path(
            image_repo="registry.example.io/org-name/zfs.kinoite-complex",
            registries_dir=Path("/tmp/registries.d"),
        )

        self.assertEqual(
            registry_file.name,
            "registry.example.io-org-name-zfs.kinoite-complex.yaml",
        )


if __name__ == "__main__":
    unittest.main()
