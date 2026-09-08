"""
Script: tests/test_common.py
What: Direct tests for shared CI helper behavior.
Doing: Mocks command wrappers and inspect helpers.
Why: Several workflow helpers rely on these common skopeo contracts.
Goal: Keep low-level image helper failure behavior clear.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ci_tools.common import (
    COSIGN_TIMEOUT,
    GIT_REMOTE_TIMEOUT,
    REGISTRY_METADATA_TIMEOUT,
    REGISTRY_RETRY_ATTEMPTS,
    REGISTRY_RETRY_DELAY_SECONDS,
    REGISTRY_TRANSFER_TIMEOUT,
    SECRET_ARG_FLAGS,
    CiToolError,
    cosign_verify,
    git_ls_remote_resolve,
    is_missing_image_error,
    redact_command_args,
    run_cmd,
    run_cmd_with_retries,
    run_json_cmd,
    skopeo_copy,
    skopeo_inspect_digest,
    skopeo_inspect_json,
    skopeo_inspect_json_optional,
    write_github_env,
    write_github_outputs,
)


def parse_github_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    lines = path.read_text(encoding="utf-8").splitlines()
    index = 0
    while index < len(lines):
        line = lines[index]
        if "<<" in line:
            key, delimiter = line.split("<<", 1)
            index += 1
            value_lines: list[str] = []
            while index < len(lines) and lines[index] != delimiter:
                value_lines.append(lines[index])
                index += 1
            values[key] = "\n".join(value_lines)
        else:
            key, value = line.split("=", 1)
            values[key] = value
        index += 1
    return values


class GitLsRemoteResolveTests(unittest.TestCase):
    BRANCH_SHA = "3333333333333333333333333333333333333333"
    LIGHTWEIGHT_SHA = "4444444444444444444444444444444444444444"
    TAG_OBJECT_SHA = "1111111111111111111111111111111111111111"
    PEELED_COMMIT_SHA = "2222222222222222222222222222222222222222"

    def _resolve(self, output: str, ref: str) -> str:
        with patch("ci_tools.common.run_cmd", return_value=output):
            return git_ls_remote_resolve("https://example.invalid/repo.git", ref)

    def test_resolves_branch_ref(self) -> None:
        output = f"{self.BRANCH_SHA}\trefs/heads/main\n"
        self.assertEqual(self._resolve(output, "main"), self.BRANCH_SHA)

    def test_resolves_lightweight_tag_ref(self) -> None:
        output = f"{self.LIGHTWEIGHT_SHA}\trefs/tags/v1.0.0\n"
        self.assertEqual(self._resolve(output, "v1.0.0"), self.LIGHTWEIGHT_SHA)

    def test_annotated_tag_resolves_to_peeled_commit(self) -> None:
        # An annotated tag lists the tag object first, then the peeled `^{}`
        # commit. The commit is what `git checkout` lands on later, so it must
        # be the value returned here.
        output = (
            f"{self.TAG_OBJECT_SHA}\trefs/tags/v2.4.0\n"
            f"{self.PEELED_COMMIT_SHA}\trefs/tags/v2.4.0^{{}}\n"
        )
        self.assertEqual(self._resolve(output, "v2.4.0"), self.PEELED_COMMIT_SHA)

    def test_branch_preferred_over_same_named_tag(self) -> None:
        output = (
            f"{self.BRANCH_SHA}\trefs/heads/main\n"
            f"{self.LIGHTWEIGHT_SHA}\trefs/tags/main\n"
        )
        self.assertEqual(self._resolve(output, "main"), self.BRANCH_SHA)

    def test_raises_when_no_resolvable_sha(self) -> None:
        with self.assertRaises(CiToolError):
            self._resolve("not-a-sha\trefs/heads/main\n", "main")


class CommonTests(unittest.TestCase):
    def test_skopeo_inspect_digest_requires_digest_field(self) -> None:
        with patch(
            "ci_tools.common.skopeo_inspect_json",
            return_value={"Name": "example"},
        ), self.assertRaises(CiToolError) as context:
            skopeo_inspect_digest("docker://ghcr.io/example/image:tag")

        self.assertIn("docker://ghcr.io/example/image:tag", str(context.exception))

    def test_is_missing_image_error_matches_known_markers(self) -> None:
        self.assertTrue(is_missing_image_error("manifest unknown"))
        self.assertTrue(is_missing_image_error("Error: reading manifest: name unknown"))
        self.assertTrue(is_missing_image_error("404 Not Found"))
        self.assertFalse(is_missing_image_error("unauthorized: authentication required"))

    def test_skopeo_inspect_json_optional_returns_none_for_missing_image(self) -> None:
        with patch(
            "ci_tools.common.skopeo_inspect_json",
            side_effect=CiToolError("manifest unknown"),
        ):
            self.assertIsNone(
                skopeo_inspect_json_optional("docker://ghcr.io/example/image:tag")
            )

    def test_skopeo_inspect_json_optional_reraises_other_errors(self) -> None:
        with patch(
            "ci_tools.common.skopeo_inspect_json",
            side_effect=CiToolError("unauthorized: authentication required"),
        ), self.assertRaises(CiToolError):
            skopeo_inspect_json_optional("docker://ghcr.io/example/image:tag")

    def test_skopeo_inspect_json_optional_returns_result_on_success(self) -> None:
        with patch(
            "ci_tools.common.skopeo_inspect_json",
            return_value={"Digest": "sha256:abc"},
        ):
            self.assertEqual(
                skopeo_inspect_json_optional("docker://ghcr.io/example/image:tag", creds="a:b"),
                {"Digest": "sha256:abc"},
            )

    def test_skopeo_copy_omits_digest_flags_by_default(self) -> None:
        with patch("ci_tools.common.run_cmd") as run_cmd_mock:
            skopeo_copy("docker://src:tag", "docker://dst:tag")

        args = run_cmd_mock.call_args.args[0]
        self.assertNotIn("--preserve-digests", args)
        self.assertFalse(any(arg.startswith("--multi-arch=") for arg in args))

    def test_skopeo_copy_adds_preserve_digests_and_multi_arch_when_requested(self) -> None:
        with patch("ci_tools.common.run_cmd") as run_cmd_mock:
            skopeo_copy(
                "docker://src:tag",
                "docker://dst:tag",
                preserve_digests=True,
                multi_arch="all",
            )

        args = run_cmd_mock.call_args.args[0]
        self.assertIn("--preserve-digests", args)
        self.assertIn("--multi-arch=all", args)

    def test_cosign_verify_does_not_pass_new_bundle_format_flag(self) -> None:
        # cosign v2.4.1 (preinstalled in the akmods build container) does not
        # recognize --new-bundle-format at all, and v3.1.2 verifies this
        # repo's legacy-format signatures fine without it -- verified against
        # both real binaries against a real signed image. This call must work
        # unmodified in either environment.
        with patch("ci_tools.common.run_cmd") as run_cmd_mock:
            cosign_verify("ghcr.io/example/image@sha256:abc", key_path="/tmp/cosign.pub")

        args = run_cmd_mock.call_args.args[0]
        self.assertEqual(
            args, ["cosign", "verify", "--key", "/tmp/cosign.pub", "ghcr.io/example/image@sha256:abc"]
        )
        self.assertNotIn("--new-bundle-format=false", args)

    def test_cosign_verify_propagates_failure(self) -> None:
        with patch(
            "ci_tools.common.run_cmd",
            side_effect=CiToolError("no signatures found"),
        ), self.assertRaises(CiToolError) as context:
            cosign_verify("ghcr.io/example/image@sha256:abc", key_path="/tmp/cosign.pub")

        self.assertIn("no signatures found", str(context.exception))

    def test_cosign_verify_passes_explicit_registry_credentials_when_provided(self) -> None:
        with patch("ci_tools.common.run_cmd") as run_cmd_mock:
            cosign_verify(
                "ghcr.io/example/image@sha256:abc",
                key_path="/tmp/cosign.pub",
                registry_username="Danathar",
                registry_password="token",
            )

        args = run_cmd_mock.call_args.args[0]
        self.assertEqual(
            args,
            [
                "cosign",
                "verify",
                "--key",
                "/tmp/cosign.pub",
                "--registry-username",
                "Danathar",
                "--registry-password",
                "token",
                "ghcr.io/example/image@sha256:abc",
            ],
        )

    def test_run_cmd_redacts_secret_args_in_failure_message(self) -> None:
        args = [
            "skopeo",
            "copy",
            "--src-creds",
            "actor:src-secret",
            "--dest-creds=actor:dest-secret",
            "--registry-username",
            "secret-user",
            "--registry-password=secret-password",
        ]
        error = subprocess.CalledProcessError(
            1,
            args,
            output="",
            stderr="failed",
        )
        with (
            patch("ci_tools.common.subprocess.run", side_effect=error),
            self.assertRaises(CiToolError) as context,
        ):
            run_cmd(args)

        message = str(context.exception)
        self.assertNotIn("src-secret", message)
        self.assertNotIn("dest-secret", message)
        self.assertNotIn("secret-user", message)
        self.assertNotIn("secret-password", message)
        self.assertIn("--src-creds ***REDACTED***", message)
        self.assertIn("--dest-creds=***REDACTED***", message)

    def test_run_json_cmd_redacts_secret_args_in_failure_message(self) -> None:
        with (
            patch("ci_tools.common.run_cmd", return_value="not-json"),
            self.assertRaises(CiToolError) as context,
        ):
            run_json_cmd(["skopeo", "inspect", "--creds", "actor:json-secret"])

        message = str(context.exception)
        self.assertNotIn("json-secret", message)
        self.assertIn("--creds ***REDACTED***", message)

    def test_run_cmd_passes_timeout_through_to_subprocess(self) -> None:
        with patch("ci_tools.common.subprocess.run") as subprocess_run:
            subprocess_run.return_value = subprocess.CompletedProcess([], 0, stdout="ok")
            run_cmd(["skopeo", "inspect", "example"], timeout=12.5)

        self.assertEqual(subprocess_run.call_args.kwargs["timeout"], 12.5)

    def test_run_cmd_defaults_to_no_timeout(self) -> None:
        # Callers with no defensible ceiling must keep waiting rather than
        # inherit an arbitrary one.
        with patch("ci_tools.common.subprocess.run") as subprocess_run:
            subprocess_run.return_value = subprocess.CompletedProcess([], 0, stdout="ok")
            run_cmd(["rpm", "-E", "%fedora"])

        self.assertIsNone(subprocess_run.call_args.kwargs["timeout"])

    def test_run_cmd_raises_ci_tool_error_on_timeout(self) -> None:
        # A hung child must fail the job the same way a nonzero exit does,
        # not escape as a bare subprocess.TimeoutExpired traceback.
        with (
            patch(
                "ci_tools.common.subprocess.run",
                side_effect=subprocess.TimeoutExpired(["skopeo", "inspect"], 120.0),
            ),
            self.assertRaises(CiToolError) as context,
        ):
            run_cmd(["skopeo", "inspect", "example"], timeout=120.0)

        self.assertIn("timed out", str(context.exception))

    def test_run_cmd_redacts_secret_args_in_timeout_message(self) -> None:
        args = ["skopeo", "copy", "--src-creds", "actor:timeout-secret"]
        with (
            patch(
                "ci_tools.common.subprocess.run",
                side_effect=subprocess.TimeoutExpired(args, 30.0),
            ),
            self.assertRaises(CiToolError) as context,
        ):
            run_cmd(args, timeout=30.0)

        message = str(context.exception)
        self.assertNotIn("timeout-secret", message)
        self.assertIn("--src-creds ***REDACTED***", message)

    def test_timeout_message_is_not_classified_as_a_missing_image(self) -> None:
        # This is the one that matters for reuse decisions:
        # skopeo_inspect_json_optional classifies inspect failures by message
        # text, and returning None for a timeout would turn "we could not
        # tell" into "the image does not exist" -- which downstream code reads
        # as a definitive answer.
        with (
            patch(
                "ci_tools.common.subprocess.run",
                side_effect=subprocess.TimeoutExpired(["skopeo", "inspect"], 120.0),
            ),
            self.assertRaises(CiToolError) as context,
        ):
            run_cmd(["skopeo", "inspect", "example"], timeout=120.0)

        message = str(context.exception)
        self.assertFalse(is_missing_image_error(message))

    def test_skopeo_inspect_json_optional_reraises_timeouts(self) -> None:
        # End-to-end form of the check above, through the real classifier.
        with (
            patch(
                "ci_tools.common.subprocess.run",
                side_effect=subprocess.TimeoutExpired(["skopeo", "inspect"], 120.0),
            ),
            self.assertRaises(CiToolError),
        ):
            skopeo_inspect_json_optional("ghcr.io/example/image:tag")

    def test_registry_and_signing_helpers_bound_their_calls(self) -> None:
        # Each helper that talks to a registry, a git remote, or a signature
        # store passes a ceiling. Asserting the constant rather than a literal
        # keeps this test about "is it bounded", not about the exact number.
        cases = (
            (
                "inspect",
                lambda: skopeo_inspect_digest("ghcr.io/example/image:tag"),
                REGISTRY_METADATA_TIMEOUT,
            ),
            (
                "copy",
                lambda: skopeo_copy("docker://a", "docker://b"),
                REGISTRY_TRANSFER_TIMEOUT,
            ),
            (
                "ls-remote",
                lambda: git_ls_remote_resolve("https://example.invalid/r.git", "main"),
                GIT_REMOTE_TIMEOUT,
            ),
            (
                "cosign-verify",
                lambda: cosign_verify("ghcr.io/example/image@sha256:x", key_path="cosign.pub"),
                COSIGN_TIMEOUT,
            ),
        )
        for label, invoke, expected_timeout in cases:
            with self.subTest(helper=label):
                with patch("ci_tools.common.run_cmd") as run_cmd_mock:
                    run_cmd_mock.return_value = (
                        '{"Digest": "sha256:x"}'
                        if label == "inspect"
                        else "0000000000000000000000000000000000000000\trefs/heads/main"
                    )
                    invoke()

                self.assertEqual(
                    run_cmd_mock.call_args.kwargs.get("timeout"), expected_timeout
                )

    def test_write_github_outputs_uses_safe_heredoc_values(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_file = Path(temp_dir) / "output"
            with patch.dict(os.environ, {"GITHUB_OUTPUT": str(output_file)}, clear=False):
                write_github_outputs(
                    {
                        "single": "value",
                        "newline": "first\nsecond",
                        "equals": "a=b",
                        "literal_eof": "contains EOF text",
                    }
                )

            self.assertEqual(
                parse_github_file(output_file),
                {
                    "single": "value",
                    "newline": "first\nsecond",
                    "equals": "a=b",
                    "literal_eof": "contains EOF text",
                },
            )

    def test_write_github_env_uses_safe_heredoc_values(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            env_file = Path(temp_dir) / "env"
            with patch.dict(os.environ, {"GITHUB_ENV": str(env_file)}, clear=False):
                write_github_env(
                    {
                        "SINGLE": "value",
                        "NEWLINE": "first\nsecond",
                        "EQUALS": "a=b",
                        "LITERAL_EOF": "contains EOF text",
                    }
                )

            self.assertEqual(
                parse_github_file(env_file),
                {
                    "SINGLE": "value",
                    "NEWLINE": "first\nsecond",
                    "EQUALS": "a=b",
                    "LITERAL_EOF": "contains EOF text",
                },
            )


class CredentialArgvTests(unittest.TestCase):
    """
    Where the registry credential lands in the argv these helpers build.

    `redact_command_args` keeps secrets out of a failure message, and that is
    well covered -- but it only redacts values sitting behind a flag named in
    `SECRET_ARG_FLAGS`. A credential appended positionally is redacted by
    nothing and prints verbatim into the job log the first time the command
    fails. So the redaction tests cannot stand in for these: the mutation they
    cannot catch is exactly the one that moves the value out from behind its
    flag.

    Every caller of `skopeo_inspect_json` and `skopeo_copy` mocks the helper
    rather than the process, so until now nothing executed the two lines that
    assemble those flags.
    """

    _CREDS = "Danathar:registry-token-value"
    _SECRET = "registry-token-value"

    def assert_secret_is_redactable(self, argv: list[str], secret: str) -> None:
        """Assert every occurrence of `secret` sits behind a redacted flag."""
        self.assertTrue(
            any(secret in arg for arg in argv),
            f"{secret!r} does not appear in {argv!r}; the test is asserting nothing",
        )
        for index, arg in enumerate(argv):
            if secret not in arg:
                continue
            if "=" in arg and arg.split("=", 1)[0] in SECRET_ARG_FLAGS:
                continue
            self.assertGreater(
                index, 0, f"{arg!r} is the command name, so no flag can precede it"
            )
            self.assertIn(
                argv[index - 1],
                SECRET_ARG_FLAGS,
                f"{arg!r} is preceded by {argv[index - 1]!r}, which redaction ignores",
            )
        # The property all of the above exists to produce.
        self.assertNotIn(secret, " ".join(redact_command_args(argv)))

    def test_skopeo_inspect_json_puts_the_credential_behind_creds(self) -> None:
        with patch("ci_tools.common.run_json_cmd") as run_json_cmd_mock:
            skopeo_inspect_json(
                "docker://ghcr.io/example/image:tag", creds=self._CREDS
            )

        argv = run_json_cmd_mock.call_args.args[0]
        self.assertEqual(
            argv,
            [
                "skopeo",
                "inspect",
                "--creds",
                self._CREDS,
                "docker://ghcr.io/example/image:tag",
            ],
        )
        self.assert_secret_is_redactable(argv, self._SECRET)
        self.assertEqual(
            run_json_cmd_mock.call_args.kwargs["timeout"], REGISTRY_METADATA_TIMEOUT
        )

    def test_skopeo_inspect_json_omits_creds_when_none_is_given(self) -> None:
        # The anonymous form has to stay anonymous: `--creds` with an empty
        # value is not the same request, and skopeo rejects it.
        with patch("ci_tools.common.run_json_cmd") as run_json_cmd_mock:
            skopeo_inspect_json("docker://ghcr.io/example/image:tag")

        self.assertEqual(
            run_json_cmd_mock.call_args.args[0],
            ["skopeo", "inspect", "docker://ghcr.io/example/image:tag"],
        )

    def test_skopeo_copy_puts_the_credential_behind_src_and_dest_creds(self) -> None:
        with patch("ci_tools.common.run_cmd") as run_cmd_mock:
            skopeo_copy("docker://src:tag", "docker://dst:tag", creds=self._CREDS)

        argv = run_cmd_mock.call_args.args[0]
        # Both ends are authenticated: a copy between two references in the
        # same private registry needs the credential on each side. Assert the
        # flags exist before indexing off them, so dropping one reads as a
        # failed assertion rather than a ValueError from `.index`.
        self.assertIn("--src-creds", argv)
        self.assertIn("--dest-creds", argv)
        self.assertEqual(argv[argv.index("--src-creds") + 1], self._CREDS)
        self.assertEqual(argv[argv.index("--dest-creds") + 1], self._CREDS)
        # Source and destination stay last, after the flags.
        self.assertEqual(argv[-2:], ["docker://src:tag", "docker://dst:tag"])
        self.assert_secret_is_redactable(argv, self._SECRET)

    def test_skopeo_copy_omits_credential_flags_when_none_is_given(self) -> None:
        with patch("ci_tools.common.run_cmd") as run_cmd_mock:
            skopeo_copy("docker://src:tag", "docker://dst:tag")

        argv = run_cmd_mock.call_args.args[0]
        self.assertNotIn("--src-creds", argv)
        self.assertNotIn("--dest-creds", argv)


class RunCmdEnvironmentTests(unittest.TestCase):
    """
    `run_cmd`s environment merge and its no-capture return.

    The merge exists for one caller: `sign_image` passes `COSIGN_PASSWORD` and
    `COSIGN_PRIVATE_KEY` so cosign reads the key as `env://COSIGN_PRIVATE_KEY`
    instead of from argv, which is what keeps the private key out of the
    process table. `tests/test_sign_image.py` substitutes a fake command
    runner and asserts the kwargs it receives, so the merge itself never ran
    under test. Dropping the `dict(os.environ)` half hands cosign two
    variables and no `PATH`; dropping the `update` half means the key never
    arrives at all.
    """

    def test_env_overrides_are_layered_on_top_of_the_process_environment(self) -> None:
        with patch.dict(os.environ, {"AMBIENT": "from-os-environ"}, clear=False), patch(
            "ci_tools.common.subprocess.run"
        ) as subprocess_run:
            subprocess_run.return_value = subprocess.CompletedProcess([], 0, stdout="ok")
            run_cmd(["cosign", "sign"], env={"COSIGN_PASSWORD": "injected"})

        command_env = subprocess_run.call_args.kwargs["env"]
        self.assertEqual(command_env["COSIGN_PASSWORD"], "injected")
        # Without the inherited half, the child would run with no PATH.
        self.assertEqual(command_env["AMBIENT"], "from-os-environ")
        self.assertIn("PATH", command_env)

    def test_env_overrides_win_over_a_variable_of_the_same_name(self) -> None:
        with patch.dict(os.environ, {"COSIGN_PASSWORD": "stale"}, clear=False), patch(
            "ci_tools.common.subprocess.run"
        ) as subprocess_run:
            subprocess_run.return_value = subprocess.CompletedProcess([], 0, stdout="ok")
            run_cmd(["cosign", "sign"], env={"COSIGN_PASSWORD": "fresh"})

        self.assertEqual(
            subprocess_run.call_args.kwargs["env"]["COSIGN_PASSWORD"], "fresh"
        )

    def test_the_merge_does_not_mutate_the_process_environment(self) -> None:
        # The override is command-specific by contract: a secret handed to one
        # child must not leak into every later command in the same job.
        with patch("ci_tools.common.subprocess.run") as subprocess_run:
            subprocess_run.return_value = subprocess.CompletedProcess([], 0, stdout="ok")
            run_cmd(["cosign", "sign"], env={"COSIGN_PRIVATE_KEY": "key-material"})

        self.assertNotIn("COSIGN_PRIVATE_KEY", os.environ)

    def test_no_env_argument_inherits_the_environment_untouched(self) -> None:
        with patch("ci_tools.common.subprocess.run") as subprocess_run:
            subprocess_run.return_value = subprocess.CompletedProcess([], 0, stdout="ok")
            run_cmd(["skopeo", "inspect", "example"])

        # None means "inherit", which is not the same as passing a copy.
        self.assertIsNone(subprocess_run.call_args.kwargs["env"])

    def test_not_capturing_output_returns_an_empty_string(self) -> None:
        # Every `skopeo copy`, every `just` target and the cosign sign call go
        # through this path. `subprocess.run` leaves `stdout` as None when it
        # is not capturing, so returning `result.stdout` here would hand the
        # caller a None where the signature promises a str.
        with patch("ci_tools.common.subprocess.run") as subprocess_run:
            subprocess_run.return_value = subprocess.CompletedProcess([], 0, stdout=None)
            result = run_cmd(["skopeo", "copy", "a", "b"], capture_output=False)

        self.assertEqual(result, "")
        self.assertIs(subprocess_run.call_args.kwargs["capture_output"], False)

class RunCmdWithRetriesTests(unittest.TestCase):
    """
    The retry wrapper around a transient registry read.

    Every case patches `ci_tools.common.run_cmd`, so nothing here runs a real
    command, and patches `time.sleep`, so the delay is asserted rather than
    waited out. What matters is the count: how many attempts a caller gets, and
    that a caller who asked for retries never silently gets one attempt.
    """

    def test_a_first_attempt_that_succeeds_runs_once_and_does_not_sleep(self) -> None:
        with (
            patch("ci_tools.common.run_cmd", return_value="output\n") as run_cmd_mock,
            patch("ci_tools.common.time.sleep") as sleep_mock,
        ):
            result = run_cmd_with_retries(["podman", "pull", "example"])

        self.assertEqual(result, "output\n")
        self.assertEqual(run_cmd_mock.call_count, 1)
        sleep_mock.assert_not_called()

    def test_a_transient_failure_is_retried_and_its_later_success_returned(self) -> None:
        # The failure this exists for: one truncated blob, then a clean pull.
        with (
            patch(
                "ci_tools.common.run_cmd",
                side_effect=[CiToolError("unexpected EOF"), "output\n"],
            ) as run_cmd_mock,
            patch("ci_tools.common.time.sleep") as sleep_mock,
        ):
            result = run_cmd_with_retries(["podman", "pull", "example"])

        self.assertEqual(result, "output\n")
        self.assertEqual(run_cmd_mock.call_count, 2)
        sleep_mock.assert_called_once_with(REGISTRY_RETRY_DELAY_SECONDS)

    def test_every_attempt_failing_raises_naming_the_count_and_the_last_error(self) -> None:
        with (
            patch(
                "ci_tools.common.run_cmd",
                side_effect=CiToolError("unexpected EOF"),
            ) as run_cmd_mock,
            patch("ci_tools.common.time.sleep") as sleep_mock,
            self.assertRaises(CiToolError) as caught,
        ):
            run_cmd_with_retries(["podman", "pull", "example"], attempts=3)

        self.assertEqual(run_cmd_mock.call_count, 3)
        # Sleeps happen between attempts, not after the last one.
        self.assertEqual(sleep_mock.call_count, 2)
        self.assertEqual(
            str(caught.exception),
            "Command failed after 3 attempts: unexpected EOF",
        )

    def test_a_timeout_is_retried_like_any_other_transient_failure(self) -> None:
        # `run_cmd` raises CiToolError for a timeout too. A stalled transfer and
        # a truncated one are the same class here, so both get the same budget.
        with (
            patch(
                "ci_tools.common.run_cmd",
                side_effect=[CiToolError("Command timed out after 1800.0s: podman pull"), ""],
            ) as run_cmd_mock,
            patch("ci_tools.common.time.sleep"),
        ):
            run_cmd_with_retries(["podman", "pull", "example"])

        self.assertEqual(run_cmd_mock.call_count, 2)

    def test_keyword_arguments_reach_run_cmd_unchanged(self) -> None:
        # The wrapper must not swallow the timeout or the capture setting: a
        # retried transfer with no ceiling is the hang this repo already guards.
        with (
            patch("ci_tools.common.run_cmd", return_value="") as run_cmd_mock,
            patch("ci_tools.common.time.sleep"),
        ):
            run_cmd_with_retries(
                ["podman", "pull", "example"],
                capture_output=False,
                timeout=REGISTRY_TRANSFER_TIMEOUT,
            )

        self.assertEqual(run_cmd_mock.call_args.args[0], ["podman", "pull", "example"])
        self.assertEqual(
            run_cmd_mock.call_args.kwargs,
            {"capture_output": False, "timeout": REGISTRY_TRANSFER_TIMEOUT},
        )

    def test_a_nonsense_attempt_count_is_rejected_rather_than_run_once(self) -> None:
        # `attempts=0` would otherwise fall out of the loop and raise the
        # unreachable branch, reporting a failure that never ran.
        with (
            patch("ci_tools.common.run_cmd") as run_cmd_mock,
            self.assertRaises(ValueError),
        ):
            run_cmd_with_retries(["podman", "pull", "example"], attempts=0)

        run_cmd_mock.assert_not_called()

    def test_the_default_attempt_count_is_the_registry_constant(self) -> None:
        with (
            patch(
                "ci_tools.common.run_cmd",
                side_effect=CiToolError("unexpected EOF"),
            ) as run_cmd_mock,
            patch("ci_tools.common.time.sleep"),
            self.assertRaises(CiToolError),
        ):
            run_cmd_with_retries(["podman", "pull", "example"])

        self.assertEqual(run_cmd_mock.call_count, REGISTRY_RETRY_ATTEMPTS)
if __name__ == "__main__":
    unittest.main()
