"""
Script: tests/test_action_publish_native_image.py
What: Executes the three shell steps of .github/actions/publish-native-image/action.yml --
retag, push the transient tag, and promote the pushed digest to the requested tag.
Doing: Extracts each step's `run:` body from the composite action with PyYAML and runs it under
bash with the step's `env:` block resolved the way GitHub resolves it, against stub `buildah`,
`podman` and `skopeo` binaries that record their argv and model a tiny tag-to-digest registry.
Why: This action owns the publication sequence that keeps a user-facing tag from ever pointing
at unsigned content: push a transient tag, sign that digest, then promote *that digest* to the
requested tag and verify the promoted tag resolves to it. All of that is shell inside a
composite action, and no tier runs it -- tests/test_workflow_build_container.py only text-matches
the action's `if:` guard, `tests/e2e/` dispatches `ci_tools.cli` commands and never a workflow
step, and the composite actions are outside `tests/check_coverage.py`'s manifest by design.
A promotion that copied from the mutable transient *tag* instead of the signed digest, that
dropped `--preserve-digests`, or whose digest cross-check stopped failing the step would publish
exactly the thing this sequence exists to prevent, with every existing test still green.
Goal: Make "the tag users pull resolves to the digest this run signed" fail here rather than in
the registry, where the only symptom is a signature that does not match what is being pulled.

The steps' text is executed rather than copied. A renamed or deleted step fails the extraction
loudly instead of leaving this file silently asserting nothing.

Nothing here reaches a registry. `buildah`, `podman` and `skopeo` are stubs; PATH deliberately
omits the directories where the real tools live, so a step that stopped being stubbed would fail
rather than talk to ghcr.io. The stub registry is a directory of files mapping a reference to the
digest it resolves to, which is enough to tell "copied the digest" from "copied the tag".

PyYAML is a transitive pytest dependency and present in CI (see .github/workflows/test.yml).
The import is guarded so the suite still runs under `python3 -m unittest discover -s tests`
with nothing installed, matching tests/test_workflow_build_container.py.
"""

from __future__ import annotations

import re
import subprocess
import tempfile
import unittest
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover - exercised only where PyYAML is absent
    yaml = None

REPO_ROOT = Path(__file__).resolve().parent.parent
ACTION_PATH = REPO_ROOT / ".github" / "actions" / "publish-native-image" / "action.yml"

RETAG_STEP = "Retag local image for transient publication"
PUSH_STEP = "Push transient tag with podman"
PROMOTE_STEP = "Promote pushed digest to requested tag"
SIGN_STEP = "Sign transient image digest"

# PATH must reach the stubs and must NOT reach a real buildah/podman/skopeo, which live in
# /usr/bin on a runner. Only the stub directory plus a shell is offered, so an unstubbed call
# fails with "command not found" instead of contacting a registry.
SAFE_PATH = "/usr/bin:/bin"

REGISTRY = "ghcr.io/danathar"
IMAGE = "zfs-kinoite-complex"
TAG = "latest"
RUN_ID = "34266369977"
TRANSIENT_TAG = f"{TAG}-unsigned-{RUN_ID}"

SIGNED_DIGEST = "sha256:" + "a" * 64
STALE_DIGEST = "sha256:" + "b" * 64
REENCODED_DIGEST = "sha256:" + "c" * 64

REGISTRY_USER = "danathar"
REGISTRY_PASSWORD = "not-a-real-token-1234"

# Records argv, then answers from $STUB_REGISTRY: one file per reference, holding the digest
# that reference resolves to. `inspect` prints it (and fails when the reference is absent, as
# skopeo does); `copy` resolves the source and writes the destination, which is what makes
# "promoted the signed digest" distinguishable from "promoted whatever the tag points at now".
# $STUB_COPY_DIGEST overrides the digest a copy lands, standing in for a manifest re-encode.
STUB = r"""#!/bin/sh
printf '%s\n' "$*" >> "${STUB_CALLS}"
key() { printf '%s' "$1" | tr -c 'A-Za-z0-9' '_'; }
tool=$(basename "$0")
if [ "${tool}" != "skopeo" ]; then
  exit 0
fi
subcmd=$1
shift
last=""
prev=""
for arg in "$@"; do
  prev=${last}
  last=${arg}
done
case "${subcmd}" in
  inspect)
    file="${STUB_REGISTRY}/$(key "${last}")"
    if [ ! -f "${file}" ]; then
      echo "stub skopeo: no such reference ${last}" >&2
      exit 1
    fi
    cat "${file}"
    ;;
  copy)
    source_file="${STUB_REGISTRY}/$(key "${prev}")"
    if [ ! -f "${source_file}" ]; then
      echo "stub skopeo: cannot copy absent reference ${prev}" >&2
      exit 1
    fi
    landed=$(cat "${source_file}")
    if [ -n "${STUB_COPY_DIGEST}" ]; then
      landed=${STUB_COPY_DIGEST}
    fi
    printf '%s' "${landed}" > "${STUB_REGISTRY}/$(key "${last}")"
    ;;
  *)
    echo "stub skopeo: unexpected subcommand ${subcmd}" >&2
    exit 1
    ;;
esac
"""


def _action() -> dict:
    return yaml.safe_load(ACTION_PATH.read_text(encoding="utf-8"))


def _step(name: str) -> dict:
    """
    Return the composite step called `name`.

    Located by name rather than by position: a step inserted above it must not silently move
    these tests onto different shell.
    """

    for step in _action()["runs"]["steps"]:
        if step.get("name") == name:
            return step
    raise AssertionError(
        f"publish-native-image/action.yml has no step named {name!r}; this test executes "
        "that step's shell and cannot find it"
    )


def _step_body(name: str) -> str:
    body = _step(name).get("run")
    if not body:
        raise AssertionError(
            f"publish-native-image/action.yml step {name!r} no longer has a `run:` body"
        )
    return body


class Result:
    """What a step leaves behind: its exit status, its output, and every tool call it made."""

    def __init__(self, completed: subprocess.CompletedProcess, calls: list[str]):
        self.returncode = completed.returncode
        self.stdout = completed.stdout
        self.stderr = completed.stderr
        self.calls = calls

    def calls_matching(self, needle: str) -> list[str]:
        return [call for call in self.calls if needle in call]


@unittest.skipIf(yaml is None, "PyYAML is not installed")
class PublishActionStepTests(unittest.TestCase):
    """
    The publication sequence, run as shell.

    Every case executes the action's own text with the `${{ inputs.* }}` and
    `${{ github.run_id }}` expressions resolved the way GitHub resolves them.
    """

    def _run(
        self,
        step_name: str,
        *,
        registry_contents: dict[str, str] | None = None,
        copy_digest: str = "",
    ) -> Result:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bindir = root / "bin"
            bindir.mkdir()
            for tool in ("buildah", "podman", "skopeo"):
                stub = bindir / tool
                stub.write_text(STUB, encoding="utf-8")
                stub.chmod(0o755)

            stub_registry = root / "registry"
            stub_registry.mkdir()
            for reference, digest in (registry_contents or {}).items():
                (stub_registry / re.sub(r"[^A-Za-z0-9]", "_", reference)).write_text(
                    digest, encoding="utf-8"
                )

            calls = root / "calls"
            calls.touch()

            env = {
                "PATH": f"{bindir}:{SAFE_PATH}",
                "STUB_CALLS": str(calls),
                "STUB_REGISTRY": str(stub_registry),
                "STUB_COPY_DIGEST": copy_digest,
                # The step's own `env:` block, expressions resolved.
                "IMAGE_REGISTRY": REGISTRY,
                "IMAGE_NAME": IMAGE,
                "IMAGE_TAG": TAG,
                "TRANSIENT_IMAGE_TAG": TRANSIENT_TAG,
                "REGISTRY_USER": REGISTRY_USER,
                "REGISTRY_PASSWORD": REGISTRY_PASSWORD,
            }

            completed = subprocess.run(
                ["bash", "-c", _step_body(step_name)],
                env=env,
                cwd=tmp,
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
            recorded = [
                line
                for line in calls.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            return Result(completed, recorded)

    def _promote(self, **kwargs) -> Result:
        """Promote against a registry that already holds the pushed transient tag."""

        contents = {
            f"docker://{REGISTRY}/{IMAGE}:{TRANSIENT_TAG}": SIGNED_DIGEST,
            f"docker://{REGISTRY}/{IMAGE}@{SIGNED_DIGEST}": SIGNED_DIGEST,
        }
        contents.update(kwargs.pop("registry_contents", {}))
        return self._run(PROMOTE_STEP, registry_contents=contents, **kwargs)

    # -- retag -------------------------------------------------------------

    def test_the_transient_tag_is_cut_from_the_locally_built_image(self) -> None:
        """
        The transient tag must name the image buildah just built, not a fresh pull.

        `buildah` and `podman` share local container storage, which is the whole reason the
        next step can push a tag that was never pushed: a retag that named something else
        would publish an image this run did not build.
        """

        result = self._run(RETAG_STEP)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.calls, [f"tag {IMAGE}:{TAG} {IMAGE}:{TRANSIENT_TAG}"]
        )

    # -- push --------------------------------------------------------------

    def test_the_transient_tag_is_pushed_under_the_registry_path(self) -> None:
        """
        Local name in, registry-qualified name out.

        The local reference carries no registry, so a push that forgot to prefix
        `IMAGE_REGISTRY` on the destination would push to docker.io by default.
        """

        result = self._run(PUSH_STEP)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(result.calls), 1, result.calls)
        self.assertIn(
            f"{IMAGE}:{TRANSIENT_TAG} {REGISTRY}/{IMAGE}:{TRANSIENT_TAG}",
            result.calls[0],
        )

    def test_the_push_never_publishes_the_requested_tag(self) -> None:
        """
        Nothing user-facing is pushed before the signature exists.

        The sequence's entire guarantee is that `:latest` appears only after the digest under
        it has been signed, so this step must mention the requested tag only as the stem the
        transient tag is cut from.
        """

        result = self._run(PUSH_STEP)
        self.assertNotIn(
            f"{IMAGE}:{TAG} ",
            result.calls[0] + " ",
            "the push step must publish the transient tag only",
        )

    def test_the_push_verifies_the_registry_certificate(self) -> None:
        """
        `--tls-verify=true` is spelled out rather than left to the default.

        Podman's default is already true, so dropping the flag changes nothing today and
        everything the day a runner is configured with an insecure-registry entry.
        """

        result = self._run(PUSH_STEP)
        self.assertIn("--tls-verify=true", result.calls[0])

    def test_the_registry_password_is_passed_as_a_credential_argument(self) -> None:
        """
        The token must arrive as the value of `--creds`, not loose in the argv.

        Same shape as the redaction rule in ci_tools/common.py: masking strips flags it
        recognises, so a credential smuggled into a positional argument is a credential
        printed verbatim into the job log.
        """

        credentials = f"{REGISTRY_USER}:{REGISTRY_PASSWORD}"
        arguments = self._run(PUSH_STEP).calls[0].split()
        self.assertEqual(
            arguments.count(credentials),
            1,
            f"the credential pair must appear exactly once: {arguments}",
        )
        self.assertEqual(
            arguments[arguments.index(credentials) - 1],
            "--creds",
            "the credential pair must follow --creds, never stand alone in the argv",
        )

    # -- promote -----------------------------------------------------------

    def test_the_promotion_copies_the_signed_digest_and_not_the_transient_tag(
        self,
    ) -> None:
        """
        The copy source must be `name@digest`, never `name:transient-tag`.

        A tag is mutable: copying from it promotes whatever it resolves to at copy time,
        which is not necessarily the digest cosign just signed. Copying by digest is what
        makes the signature and the published bits the same object.
        """

        result = self._promote()
        self.assertEqual(result.returncode, 0, result.stderr)
        copies = result.calls_matching("copy ")
        self.assertEqual(len(copies), 1, result.calls)
        self.assertIn(f"docker://{REGISTRY}/{IMAGE}@{SIGNED_DIGEST}", copies[0])
        self.assertNotIn(
            f"docker://{REGISTRY}/{IMAGE}:{TRANSIENT_TAG} ",
            copies[0] + " ",
            "the promotion must read the digest, not the transient tag",
        )

    def test_the_promotion_lands_on_the_requested_tag(self) -> None:
        """The destination is the tag the caller asked for, registry-qualified."""

        result = self._promote()
        copies = result.calls_matching("copy ")
        self.assertTrue(
            copies[0].endswith(f"docker://{REGISTRY}/{IMAGE}:{TAG}"),
            copies[0],
        )

    def test_the_promotion_preserves_digests_and_every_architecture(self) -> None:
        """
        `--preserve-digests` and `--multi-arch=all` are both load-bearing.

        Without the first, skopeo may re-encode the manifest and drop the layer annotations
        Chunkah writes -- the entire point of the rechunk step that runs before this one.
        Without the second, a multi-architecture image is silently narrowed to one.
        """

        result = self._promote()
        copies = result.calls_matching("copy ")
        self.assertIn("--preserve-digests", copies[0])
        self.assertIn("--multi-arch=all", copies[0])

    def test_a_digest_that_changes_in_flight_fails_the_step(self) -> None:
        """
        The cross-check is the last thing standing between a signature and the bits.

        If the promoted tag resolves to anything other than the signed digest, the step must
        fail the job rather than leave `:latest` pointing at unsigned content. Driven by
        making the copy land a different digest, which is what a manifest re-encode does.
        """

        result = self._promote(copy_digest=REENCODED_DIGEST)
        self.assertNotEqual(
            result.returncode,
            0,
            "a promoted tag that does not resolve to the signed digest must fail the step",
        )
        self.assertIn(REENCODED_DIGEST, result.stderr)
        self.assertIn(SIGNED_DIGEST, result.stderr)

    def test_the_promoted_tag_is_read_back_after_the_copy(self) -> None:
        """
        The verification must inspect the registry once the copy has landed.

        A read taken before the copy would answer for the *previous* holder of the tag --
        here a stale digest from an earlier build -- and would fail a promotion that
        succeeded, or, on a first-ever push, compare nothing against nothing.
        """

        result = self._promote(
            registry_contents={
                f"docker://{REGISTRY}/{IMAGE}:{TAG}": STALE_DIGEST,
            }
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            [call.split()[0] for call in result.calls],
            ["inspect", "copy", "inspect"],
            "the step must inspect the transient tag, copy, then read the promoted tag back",
        )

    def test_a_failed_lookup_of_the_signed_digest_fails_the_step(self) -> None:
        """
        A promotion that cannot read the transient tag must fail, not report success.

        Without `set -euo pipefail` this step failed *open*: a failed first `skopeo inspect`
        left `transient_digest` empty, the copy of `...@` failed and was ignored, and
        `final_digest` was empty too, so the guard compared "" against "" and the step exited
        0 having promoted nothing. For `:latest` that was masked -- the tag already exists, so
        the read-back returns the previous build's digest and the comparison fails closed --
        but a tag that does not exist yet, such as the per-branch `br-*` tags build-branch.yml
        publishes, would be reported as published without ever having been written.

        Driven with an empty stub registry, which is the fresh-tag case: neither the transient
        tag nor the requested tag resolves.
        """

        result = self._run(PROMOTE_STEP, registry_contents={})
        self.assertNotEqual(
            result.returncode,
            0,
            "a promotion whose digest lookup failed must fail the step, not exit 0 having "
            f"promoted nothing: {result.calls}",
        )

    def test_the_signed_digest_is_taken_from_the_transient_tag_this_job_pushed(
        self,
    ) -> None:
        """
        The first inspect names the transient tag, which only this run can have created.

        Resolving the digest from the requested tag instead would sign off on whatever the
        previous build left behind.
        """

        result = self._promote()
        self.assertIn(
            f"docker://{REGISTRY}/{IMAGE}:{TRANSIENT_TAG}",
            result.calls[0],
        )


@unittest.skipIf(yaml is None, "PyYAML is not installed")
class PublishActionSequenceTests(unittest.TestCase):
    """
    The one invariant that spans steps: the tag that is signed is the tag that is promoted.

    Each step recomputes `${{ inputs.image_tag }}-unsigned-${{ github.run_id }}` in its own
    `env:` block, so the three copies can drift apart. If the signing step's copy ever stopped
    matching the promotion's, the job would sign one image and publish another with every
    step green -- the failure this whole sequence is built to make impossible.
    """

    def _env_of(self, step_name: str) -> dict:
        return _step(step_name).get("env") or {}

    def test_the_signed_tag_and_the_promoted_transient_tag_are_the_same_expression(
        self,
    ) -> None:
        signed = self._env_of(SIGN_STEP)["IMAGE_TAG"]
        promoted = self._env_of(PROMOTE_STEP)["TRANSIENT_IMAGE_TAG"]
        self.assertEqual(signed, promoted)

    def test_every_step_derives_the_transient_tag_the_same_way(self) -> None:
        pushed = self._env_of(PUSH_STEP)["TRANSIENT_IMAGE_TAG"]
        retagged = self._env_of(RETAG_STEP)["TRANSIENT_IMAGE_TAG"]
        promoted = self._env_of(PROMOTE_STEP)["TRANSIENT_IMAGE_TAG"]
        self.assertEqual({pushed, retagged}, {promoted})

    def test_the_transient_tag_is_unique_per_run(self) -> None:
        """
        `github.run_id` in the transient tag is what keeps two concurrent runs apart.

        Without it both would push the same transient tag, and each could promote the
        other's digest -- signed, but not by the run that published it.
        """

        self.assertIn(
            "github.run_id", self._env_of(PROMOTE_STEP)["TRANSIENT_IMAGE_TAG"]
        )


if __name__ == "__main__":
    unittest.main()
