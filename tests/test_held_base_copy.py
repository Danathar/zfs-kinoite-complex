"""
Script: tests/test_held_base_copy.py
What: Tests for ci_tools/held_base_copy.py, the private copy of the held base kept in this repository's GHCR.
Doing: Mocks every registry call and checks the copy's options, its tag, its digest check, that the
GitHub token never reaches quay.io, and the one existence function the gate and the post-copy check share.
Why: A kernel hold longer than four weeks stands on this copy once quay.io deletes the held tag.
A copy at the wrong digest, a copy under the wrong tag, a token sent to the wrong registry, or a copy
the gate cannot see while the main job can would each leave the hold broken while every check stayed green.
Goal: Make each of those fail here, on the pull request that causes it.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from typing import ClassVar
from unittest.mock import patch

from ci_tools.common import CiToolError
from ci_tools.held_base_copy import (
    copy_held_base,
    held_base_copy_exists,
    held_base_copy_ref,
    main,
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


def registry(*, exists: bool = True, refusal: Exception | None = None):
    """
    A fake `skopeo_inspect_json_optional` for the private copy: it answers only with the token.

    `exists` False reads as the registry's "manifest unknown"; `refusal` is raised instead.
    """

    def optional(ref: str, *, creds: str | None = None) -> dict | None:
        assert creds == CREDS, f"the private copy was read without the token: {creds!r}"
        if refusal is not None:
            raise refusal
        return {"Digest": DIGEST} if exists else None

    return optional


class HeldBaseCopyRefTests(unittest.TestCase):
    def test_takes_the_digest_of_the_pinned_base(self) -> None:
        self.assertEqual(held_base_copy_ref(ORG, f"{SOURCE_NAME}@{DIGEST}"), COPY_BY_DIGEST)

    def test_a_ref_without_a_digest_names_no_copy(self) -> None:
        for value in ("", f"{SOURCE_NAME}:44", f"{SOURCE_NAME}@md5:abc"):
            with self.subTest(value=value):
                self.assertEqual(held_base_copy_ref(ORG, value), "")


class HeldBaseCopyExistsTests(unittest.TestCase):
    def exists(self, **kwargs) -> bool:
        with patch(
            "ci_tools.held_base_copy.skopeo_inspect_json_optional", side_effect=registry(**kwargs)
        ):
            return held_base_copy_exists(COPY_BY_DIGEST, creds=CREDS)

    def test_a_readable_copy_exists(self) -> None:
        self.assertTrue(self.exists())

    def test_a_missing_copy_does_not(self) -> None:
        self.assertFalse(self.exists(exists=False))

    def test_a_refusal_is_not_confirmed_readable_and_never_raises(self) -> None:
        # GHCR may answer an authenticated read of a package that was never created with
        # `denied`, so a refusal reads the same as missing: build, never skip, never raise.
        for message in (
            "denied: permission_denied",
            "unauthorized: authentication required",
            "403 Forbidden",
            "401 Unauthorized",
        ):
            with self.subTest(message=message):
                self.assertFalse(self.exists(refusal=CiToolError(message)))

    def test_any_other_registry_error_raises(self) -> None:
        # "Could not tell" is never read as "missing".
        for message in ("toomanyrequests", "connection reset by peer"):
            with self.subTest(message=message), self.assertRaises(CiToolError):
                self.exists(refusal=CiToolError(message))

    def test_it_is_always_asked_with_the_token(self) -> None:
        with patch(
            "ci_tools.held_base_copy.skopeo_inspect_json_optional", return_value=None
        ) as inspect:
            held_base_copy_exists(COPY_BY_DIGEST, creds=CREDS)
        inspect.assert_called_once_with(COPY_BY_DIGEST, creds=CREDS)


class CopyHeldBaseTests(unittest.TestCase):
    def run_copy(
        self,
        *,
        tag_digest: str | None = None,
        copied_digest: str = DIGEST,
        version: str = VERSION,
        base_image_name: str = SOURCE_NAME,
        by_digest_readable: bool = True,
        by_digest_refusal: Exception | None = None,
    ):
        """
        Run `copy_held_base` against a fake registry, with the real `held_base_copy_exists`.

        `tag_digest` is what the copy's version tag already holds (None: nothing yet).
        `copied_digest` is what that tag holds after a copy. `by_digest_readable` and
        `by_digest_refusal` say what the token sees when it reads the copy by digest.
        """

        def optional(ref: str, *, creds: str | None = None) -> dict | None:
            self.assertEqual(creds, CREDS, f"{ref} was read without the token")
            if ref == COPY_BY_TAG:
                return None if tag_digest is None else {"Digest": tag_digest}
            self.assertEqual(ref, COPY_BY_DIGEST)
            if by_digest_refusal is not None:
                raise by_digest_refusal
            return {"Digest": DIGEST} if by_digest_readable else None

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
        self.assertEqual(self.run_copy(), COPY_BY_DIGEST)
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

    def test_the_quay_source_is_read_anonymously_and_its_auth_file_holds_no_quay_entry(
        self,
    ) -> None:
        # Not a mock of skopeo_copy: the real one builds the auth file, and the command it
        # would run is captured. The GitHub token must reach ghcr.io only.
        seen: dict = {}

        def capture(argv, **_kwargs):
            self.assertNotIn("--src-authfile", argv)
            self.assertNotIn("--src-creds", argv)
            auth_path = argv[argv.index("--dest-authfile") + 1]
            seen["auths"] = json.loads(Path(auth_path).read_text(encoding="utf-8"))["auths"]
            seen["argv"] = list(argv)
            return ""

        def optional(ref: str, *, creds: str | None = None) -> dict | None:
            return None if ref == COPY_BY_TAG else {"Digest": DIGEST}

        with (
            patch(
                "ci_tools.held_base_copy.skopeo_inspect_json",
                return_value={"Labels": {"org.opencontainers.image.version": VERSION}},
            ),
            patch("ci_tools.held_base_copy.skopeo_inspect_json_optional", side_effect=optional),
            patch("ci_tools.held_base_copy.skopeo_inspect_digest", return_value=DIGEST),
            patch("ci_tools.common.run_cmd", side_effect=capture),
            redirect_stdout(io.StringIO()),
        ):
            copy_held_base(
                base_image_name=SOURCE_NAME,
                base_image_digest=DIGEST,
                image_org=ORG,
                creds=CREDS,
            )

        self.assertEqual(list(seen["auths"]), ["ghcr.io"])
        self.assertNotIn("quay.io", json.dumps(seen["auths"]))
        self.assertEqual(seen["argv"][-2:], [SOURCE_REF, COPY_BY_TAG])
        self.assertIn("--preserve-digests", seen["argv"])
        self.assertIn("--multi-arch=all", seen["argv"])

    def test_a_copy_at_another_digest_fails_closed(self) -> None:
        with self.assertRaises(CiToolError) as caught:
            self.run_copy(copied_digest="sha256:" + "8" * 64)
        self.assertIn("digest mismatch", str(caught.exception))

    def test_a_copy_that_resolves_to_no_digest_says_so(self) -> None:
        with self.assertRaises(CiToolError) as caught:
            self.run_copy(copied_digest="")
        self.assertIn("resolves to no digest, expected", str(caught.exception))

    def test_a_copy_already_there_is_not_copied_again(self) -> None:
        self.assertEqual(self.run_copy(tag_digest=DIGEST), COPY_BY_DIGEST)
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

    def test_a_copy_the_token_cannot_read_back_fails_the_run_loudly(self) -> None:
        # The post-copy check asks the gate's own existence function. If it cannot see the copy,
        # the gate would see "missing" every night and rebuild, while this job looked green.
        for kwargs in (
            {"by_digest_readable": False},
            {"by_digest_refusal": CiToolError("denied: permission_denied")},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(CiToolError) as caught:
                self.run_copy(**kwargs)
            self.assertIn("cannot read it back", str(caught.exception))
            self.assertIn("rebuild every night", str(caught.exception))

    def test_the_post_copy_check_is_the_function_the_gate_calls(self) -> None:
        from ci_tools import check_stable_signal, held_base_copy

        self.assertIs(check_stable_signal.held_base_copy_exists, held_base_copy.held_base_copy_exists)
        with (
            patch("ci_tools.held_base_copy.held_base_copy_exists", return_value=True) as exists,
            patch(
                "ci_tools.held_base_copy.skopeo_inspect_json",
                return_value={"Labels": {"org.opencontainers.image.version": VERSION}},
            ),
            patch(
                "ci_tools.held_base_copy.skopeo_inspect_json_optional",
                return_value={"Digest": DIGEST},
            ),
            redirect_stdout(io.StringIO()),
        ):
            copy_held_base(
                base_image_name=SOURCE_NAME,
                base_image_digest=DIGEST,
                image_org=ORG,
                creds=CREDS,
            )
        exists.assert_called_once_with(COPY_BY_DIGEST, creds=CREDS)

    def test_a_run_already_on_the_copy_is_verified_not_copied_onto_itself(self) -> None:
        self.assertEqual(self.run_copy(base_image_name=COPY_REPOSITORY), COPY_BY_DIGEST)
        self.assertEqual(self.copy_calls, [])
        self.inspect.assert_not_called()
        self.digest.assert_not_called()
        self.assertIn("verified, not copied", self.out.getvalue())

    def test_a_run_on_a_copy_the_registry_does_not_return_fails(self) -> None:
        with self.assertRaises(CiToolError):
            self.run_copy(base_image_name=COPY_REPOSITORY, by_digest_readable=False)

    def test_a_non_digest_base_cannot_be_kept(self) -> None:
        with self.assertRaises(CiToolError):
            copy_held_base(
                base_image_name=SOURCE_NAME,
                base_image_digest="latest",
                image_org=ORG,
                creds=CREDS,
            )


class MainTests(unittest.TestCase):
    ENV: ClassVar[dict[str, str]] = {
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

    def test_a_held_run_without_the_registry_token_fails_rather_than_going_anonymous(self) -> None:
        env = self.ENV | {"KERNEL_HELD_FROM": "7.3.4-200.fc44.x86_64"}
        with (
            patch.dict(os.environ, env, clear=False),
            patch.dict(os.environ, {"REGISTRY_TOKEN": ""}),
            patch("ci_tools.held_base_copy.copy_held_base") as copy,
            self.assertRaises(CiToolError),
        ):
            main()
        copy.assert_not_called()

    def test_running_the_module_directly_runs_main(self) -> None:
        # `python3 -m ci_tools.held_base_copy` reaches main() only through the __main__ guard.
        env = {key: value for key, value in os.environ.items() if key != "KERNEL_HELD_FROM"}
        result = subprocess.run(
            [sys.executable, "-m", "ci_tools.held_base_copy"],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
            cwd=Path(__file__).resolve().parent.parent,
            env=env,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("This run is not held", result.stdout)


if __name__ == "__main__":
    unittest.main()
