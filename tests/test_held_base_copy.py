"""
Script: tests/test_held_base_copy.py
What: Tests for ci_tools/held_base_copy.py, the copy of the held base kept in this repository's GHCR.
Doing: Mocks every registry call and checks the copy's options, its tag, its digest check, and
the three states (missing, not public, public) the gate, the build and the triage workflow read.
Why: A kernel hold longer than four weeks stands on this copy once quay.io deletes the held tag.
A copy at the wrong digest, a copy under the wrong tag, or a private copy read as a missing one
would each leave the hold with no base while every check stayed green.
Goal: Make each of those fail here, on the pull request that causes it.
"""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from ci_tools.common import CiToolError
from ci_tools.held_base_copy import (
    COPY_MISSING,
    COPY_NOT_PUBLIC,
    COPY_PUBLIC,
    copy_held_base,
    held_base_copy_ref,
    held_base_copy_state,
    main,
    main_status,
    report_copy_state,
)
from ci_tools.resolve_build_inputs import HELD_BASE_PACKAGE

ORG = "danathar"
CREDS = "actor:token"
SOURCE_NAME = "quay.io/fedora-ostree-desktops/kinoite"
DIGEST = "sha256:" + "7" * 64
VERSION = "44.20261110.0"
COPY_REPOSITORY = f"ghcr.io/{ORG}/{HELD_BASE_PACKAGE}"
COPY_BY_TAG = f"docker://{COPY_REPOSITORY}:{VERSION}"
COPY_BY_DIGEST = f"docker://{COPY_REPOSITORY}@{DIGEST}"
SOURCE_REF = f"docker://{SOURCE_NAME}@{DIGEST}"


def registry(*, exists: bool = True, anonymous: object = "public"):
    """
    A fake `skopeo_inspect_json_optional`: with creds it answers `exists`; without, `anonymous`.

    `anonymous` is "public", None (absent), or an exception to raise.
    """

    def optional(ref: str, *, creds: str | None = None) -> dict | None:
        if creds is not None:
            assert creds == CREDS, creds
            return {"Digest": DIGEST} if exists else None
        if isinstance(anonymous, Exception):
            raise anonymous
        return None if anonymous is None else {"Digest": DIGEST}

    return optional


class HeldBaseCopyRefTests(unittest.TestCase):
    def test_takes_the_digest_of_the_pinned_base(self) -> None:
        self.assertEqual(held_base_copy_ref(ORG, f"{SOURCE_NAME}@{DIGEST}"), COPY_BY_DIGEST)

    def test_a_ref_without_a_digest_names_no_copy(self) -> None:
        for value in ("", f"{SOURCE_NAME}:44", f"{SOURCE_NAME}@md5:abc"):
            with self.subTest(value=value):
                self.assertEqual(held_base_copy_ref(ORG, value), "")


class HeldBaseCopyStateTests(unittest.TestCase):
    def state(self, **kwargs) -> str:
        with patch(
            "ci_tools.held_base_copy.skopeo_inspect_json_optional", side_effect=registry(**kwargs)
        ):
            return held_base_copy_state(COPY_BY_DIGEST, creds=CREDS)

    def test_missing_is_asked_with_the_token(self) -> None:
        self.assertEqual(self.state(exists=False), COPY_MISSING)

    def test_an_anonymous_refusal_is_not_public_never_missing(self) -> None:
        refused = CiToolError("unauthorized: authentication required")
        self.assertEqual(self.state(anonymous=refused), COPY_NOT_PUBLIC)

    def test_an_anonymous_absence_of_an_existing_copy_is_not_public(self) -> None:
        self.assertEqual(self.state(anonymous=None), COPY_NOT_PUBLIC)

    def test_a_readable_copy_is_public(self) -> None:
        self.assertEqual(self.state(), COPY_PUBLIC)

    def test_any_other_anonymous_error_raises(self) -> None:
        with self.assertRaises(CiToolError):
            self.state(anonymous=CiToolError("connection reset by peer"))

    def test_a_credentialed_registry_error_raises(self) -> None:
        with (
            patch(
                "ci_tools.held_base_copy.skopeo_inspect_json_optional",
                side_effect=CiToolError("toomanyrequests"),
            ),
            self.assertRaises(CiToolError),
        ):
            held_base_copy_state(COPY_BY_DIGEST, creds=CREDS)


class ReportCopyStateTests(unittest.TestCase):
    def test_not_public_is_a_warning_with_the_owner_steps(self) -> None:
        with redirect_stdout(io.StringIO()) as out:
            report_copy_state(COPY_BY_DIGEST, COPY_NOT_PUBLIC)
        text = out.getvalue()
        self.assertTrue(text.startswith("::warning "), text)
        self.assertIn(HELD_BASE_PACKAGE, text)
        self.assertIn("Change visibility", text)

    def test_public_is_logged_plainly(self) -> None:
        with redirect_stdout(io.StringIO()) as out:
            report_copy_state(COPY_BY_DIGEST, COPY_PUBLIC)
        self.assertEqual(out.getvalue(), f"Held base copy {COPY_BY_DIGEST}: public\n")


class CopyHeldBaseTests(unittest.TestCase):
    def run_copy(
        self,
        *,
        tag_digest: str | None = None,
        copied_digest: str = DIGEST,
        version: str = VERSION,
        base_image_name: str = SOURCE_NAME,
        exists: bool = True,
    ):
        """
        Run `copy_held_base` against a fake registry.

        `tag_digest` is what the copy's version tag already holds (None: nothing yet).
        `copied_digest` is what that tag holds after a copy.
        """

        def optional(ref: str, *, creds: str | None = None) -> dict | None:
            if ref == COPY_BY_TAG and creds is not None and self.copy_calls == []:
                return None if tag_digest is None else {"Digest": tag_digest}
            return registry(exists=exists)(ref, creds=creds)

        self.copy_calls: list = []
        with (
            patch(
                "ci_tools.held_base_copy.skopeo_inspect_json",
                return_value={"Labels": {"org.opencontainers.image.version": version}},
            ) as self.inspect,
            patch("ci_tools.held_base_copy.skopeo_inspect_json_optional", side_effect=optional),
            patch(
                "ci_tools.held_base_copy.skopeo_copy",
                side_effect=lambda *args, **kwargs: self.copy_calls.append((args, kwargs)),
            ),
            patch(
                "ci_tools.held_base_copy.skopeo_inspect_digest", return_value=copied_digest
            ) as self.digest,
            redirect_stdout(io.StringIO()) as self.out,
        ):
            return copy_held_base(
                base_image_name=base_image_name,
                base_image_digest=DIGEST,
                image_org=ORG,
                creds=CREDS,
            )

    def test_copies_with_the_promotion_options_under_the_version_label(self) -> None:
        self.assertEqual(self.run_copy(), COPY_BY_TAG)
        self.inspect.assert_called_once_with(SOURCE_REF)
        self.assertEqual(
            self.copy_calls,
            [
                (
                    (SOURCE_REF, COPY_BY_TAG),
                    {
                        "creds": CREDS,
                        "preserve_digests": True,
                        "multi_arch": "all",
                        "authenticate_source": False,
                    },
                )
            ],
        )
        self.digest.assert_called_once_with(COPY_BY_TAG, creds=CREDS)
        self.assertIn(f"Copied held base {SOURCE_REF} -> {COPY_BY_TAG}", self.out.getvalue())

    def test_a_copy_at_another_digest_fails_closed(self) -> None:
        with self.assertRaises(CiToolError) as caught:
            self.run_copy(copied_digest="sha256:" + "8" * 64)
        self.assertIn("digest mismatch", str(caught.exception))

    def test_a_copy_already_there_is_not_copied_again(self) -> None:
        self.assertEqual(self.run_copy(tag_digest=DIGEST), COPY_BY_TAG)
        self.assertEqual(self.copy_calls, [])
        self.digest.assert_not_called()

    def test_a_tag_holding_another_image_is_never_overwritten(self) -> None:
        with self.assertRaises(CiToolError) as caught:
            self.run_copy(tag_digest="sha256:" + "9" * 64)
        self.assertIn("digest mismatch", str(caught.exception))
        self.assertEqual(self.copy_calls, [])

    def test_a_base_without_a_dated_version_label_is_refused(self) -> None:
        with self.assertRaises(CiToolError) as caught:
            self.run_copy(version="latest")
        self.assertIn("not a dated base version", str(caught.exception))
        self.assertEqual(self.copy_calls, [])

    def test_a_run_already_on_the_copy_only_reports_it(self) -> None:
        self.assertEqual(self.run_copy(base_image_name=COPY_REPOSITORY), COPY_BY_DIGEST)
        self.assertEqual(self.copy_calls, [])
        self.inspect.assert_not_called()
        self.assertIn(f"Held base copy {COPY_BY_DIGEST}: public", self.out.getvalue())

    def test_a_run_on_a_copy_the_registry_denies_having_fails(self) -> None:
        with self.assertRaises(CiToolError):
            self.run_copy(base_image_name=COPY_REPOSITORY, exists=False)


class MainTests(unittest.TestCase):
    ENV = {
        "BASE_IMAGE_NAME": SOURCE_NAME,
        "BASE_IMAGE_DIGEST": DIGEST,
        "GITHUB_REPOSITORY_OWNER": "Danathar",
        "REGISTRY_ACTOR": "actor",
        "REGISTRY_TOKEN": "token",
    }

    def test_an_unheld_run_copies_nothing(self) -> None:
        with (
            patch.dict(os.environ, {"KERNEL_HELD_FROM": ""}, clear=False),
            patch("ci_tools.held_base_copy.copy_held_base") as copy,
            redirect_stdout(io.StringIO()) as out,
        ):
            main()
        copy.assert_not_called()
        self.assertIn("not held", out.getvalue())

    def test_a_held_run_copies_its_base(self) -> None:
        env = self.ENV | {"KERNEL_HELD_FROM": "7.3.4-200.fc44.x86_64"}
        with (
            patch.dict(os.environ, env, clear=False),
            patch("ci_tools.held_base_copy.copy_held_base") as copy,
        ):
            main()
        copy.assert_called_once_with(
            base_image_name=SOURCE_NAME, base_image_digest=DIGEST, image_org=ORG, creds=CREDS
        )


class MainStatusTests(unittest.TestCase):
    def status(self, inputs: dict | None, state: str = COPY_PUBLIC) -> tuple[str, list]:
        with tempfile.TemporaryDirectory() as temp_dir:
            manifest = Path(temp_dir) / "build-inputs.json"
            if inputs is not None:
                manifest.write_text(json.dumps({"inputs": inputs}), encoding="utf-8")
            env = {
                "BUILD_INPUTS_MANIFEST": str(manifest),
                "GITHUB_REPOSITORY_OWNER": "Danathar",
                "REGISTRY_ACTOR": "actor",
                "REGISTRY_TOKEN": "token",
            }
            with (
                patch.dict(os.environ, env, clear=False),
                patch("ci_tools.held_base_copy.held_base_copy_state", return_value=state) as ask,
                patch("ci_tools.held_base_copy.write_github_outputs") as write,
                redirect_stdout(io.StringIO()),
            ):
                main_status()
        return write.call_args.args[0]["state"], ask.call_args_list

    HELD = {
        "kernel_held_from": "7.3.4-200.fc44.x86_64",
        "use_input_lock": False,
        "base_image_name": SOURCE_NAME,
        "base_image_digest": DIGEST,
    }

    def test_a_held_build_reports_its_copy_state(self) -> None:
        state, asked = self.status(self.HELD, COPY_NOT_PUBLIC)
        self.assertEqual(state, COPY_NOT_PUBLIC)
        self.assertEqual(len(asked), 1)
        self.assertEqual(asked[0].args, (COPY_BY_DIGEST,))
        self.assertEqual(asked[0].kwargs, {"creds": CREDS})

    def test_nothing_to_report_without_a_hold_or_a_manifest(self) -> None:
        unheld = self.HELD | {"kernel_held_from": ""}
        replay = self.HELD | {"use_input_lock": True}
        for inputs in (None, unheld, replay):
            with self.subTest(inputs=inputs):
                state, asked = self.status(inputs)
                self.assertEqual(state, "")
                self.assertEqual(asked, [])

    def test_a_held_manifest_without_a_digest_fails(self) -> None:
        with self.assertRaises(CiToolError):
            self.status(self.HELD | {"base_image_digest": ""})


if __name__ == "__main__":
    unittest.main()
