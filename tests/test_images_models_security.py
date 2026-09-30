"""Validate manifest inputs before network requests or filesystem writes."""

from copy import deepcopy
from pathlib import Path

import httpx
import pytest

from harness import images_models as models
from harness.config import ImagesConfig


@pytest.mark.parametrize("url", [
    "http://huggingface.co/file", "ftp://github.com/file", "https://localhost/file",
    "https://127.0.0.1/file", "https://huggingface.co.evil.test/file",
    "https://evil.test/huggingface.co/file", "https://huggingface.co@evil.test/file",
    "https://user@huggingface.co/file", "https://user:pass@github.com/file",
    "https://github.com:8443/file", "https://github.com:bad/file",
    "https:///file", "//github.com/file", "https://github.com/file\r\nforged",
    "https://github.com/file\x00", "https://github.com/file\x7f",
    "https://github.com/file\x85", "https://release-assets.githubusercontent.com/file",
])
def test_download_rejects_url_before_request_or_write(tmp_path, url):
    def request(_):
        pytest.fail("rejected URL reached transport")

    dest = tmp_path / "new" / "model"
    with httpx.Client(transport=httpx.MockTransport(request)) as client:
        with pytest.raises(ValueError):
            models.download_file(url, dest, "pin", 1, client=client)
    assert not dest.parent.exists()


@pytest.mark.parametrize("target", [
    "http://github.com/file", "https://127.0.0.1/file", "https://evil.test/file",
    "https://cdn-lfs.hf.co.evil.test/file", "https://user@cdn-lfs.hf.co/file",
])
def test_download_rejects_redirect_even_if_client_follows_redirects(tmp_path, target):
    hits = []

    def request(req):
        hits.append(str(req.url))
        return httpx.Response(302, headers={"Location": target})

    with httpx.Client(transport=httpx.MockTransport(request), follow_redirects=True) as client:
        with pytest.raises(ValueError):
            models.download_file("https://github.com/file", tmp_path / "model", "pin", 1, client=client)
    assert hits == ["https://github.com/file"]
    assert not (tmp_path / "model.part").exists()


# us.aws.cdn.hf.co is where the shipped Hugging Face URLs redirected on 2026-09-30.
TRUSTED_REDIRECT_HOSTS = sorted(models.DOWNLOAD_HOSTS | {
    "us.aws.cdn.hf.co", "cas-bridge.xethub.hf.co", "cdn-lfs.huggingface.co",
    "release-assets.githubusercontent.com", "objects.githubusercontent.com"})


@pytest.mark.parametrize("host", ["hf.co.evil.example", "evilhf.co", "githubusercontent.com.evil.example",
                                  "notgithubusercontent.com", "example.com"])
def test_redirect_host_suffix_is_anchored(host):
    with pytest.raises(ValueError):
        models.validate_download_url(f"https://{host}/payload", redirect=True)


def test_cdn_hosts_are_redirect_only():
    with pytest.raises(ValueError):
        models.validate_download_url("https://us.aws.cdn.hf.co/payload")


@pytest.mark.parametrize("host", TRUSTED_REDIRECT_HOSTS)
def test_trusted_redirects_download_and_verify(tmp_path, host):
    hits = []

    def request(req):
        hits.append(str(req.url))
        if len(hits) == 1:
            return httpx.Response(302, headers={"Location": f"https://{host}/payload"})
        return httpx.Response(200, content=b"x")

    with httpx.Client(transport=httpx.MockTransport(request)) as client:
        result = models.download_file("https://github.com/file", tmp_path / "model",
                                      "2d711642b726b04401627ca9fbac32f5c8530fb1903cc4db02258717921a4881", 1,
                                      client=client)
    assert (tmp_path / "model").read_bytes() == b"x"
    assert result["bytes"] == 1
    assert hits == ["https://github.com/file", f"https://{host}/payload"]


def test_redirect_limit(tmp_path):
    hits = []

    def request(req):
        hits.append(req)
        return httpx.Response(302, headers={"Location": "/again"})

    with httpx.Client(transport=httpx.MockTransport(request)) as client:
        with pytest.raises(RuntimeError, match="redirect limit"):
            models.download_file("https://github.com/file", tmp_path / "model", "pin", 1, client=client)
    assert len(hits) == 11


BAD_PATHS = ["", ".", "..", "../escape", "a/../escape", "/absolute", "\\absolute",
             "C:\\escape", "C:escape", "C:/escape", "\\\\server\\share\\file",
             "file:stream", "file\nforged", "file\x00", "file\x7f", "a//b", "a/./b"]


@pytest.mark.parametrize("relative", BAD_PATHS)
@pytest.mark.parametrize("field", ["subdir", "filename", "alt_filename", "archive"])
def test_manifest_paths_rejected_before_download_or_extraction(tmp_path, relative, field):
    manifest = deepcopy(models.load_manifest())
    cfg = ImagesConfig(comfy_dir=str(tmp_path / "comfy"))
    if field == "archive":
        manifest["comfyui"]["pinned_portable"]["filename"] = relative
        with pytest.raises(ValueError):
            models.stage_comfyui(cfg, manifest=manifest,
                                extract=lambda *_: pytest.fail("unsafe archive extracted"),
                                free_bytes=lambda _: pytest.fail("unsafe archive reached preflight"))
    else:
        manifest["assets"]["encoder"][field] = relative
        with pytest.raises(ValueError):
            models.encoder_paths(manifest, tmp_path / "models")
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("name", ["nested/file", "nested\\file"])
def test_filenames_must_be_plain_names(tmp_path, name):
    with pytest.raises(ValueError):
        models.manifest_path(tmp_path, name, plain_name=True)


def test_resolved_path_must_stay_inside_base(tmp_path, monkeypatch):
    original = Path.resolve
    # Model a pre-existing symlink without requiring Windows symlink privileges.
    def resolve(path, *args, **kwargs):
        if path.name == "link":
            return tmp_path.parent / "outside"
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolve)
    with pytest.raises(ValueError, match="outside"):
        models.manifest_path(tmp_path, "link")


def test_shipped_manifest_urls_and_paths_pass(tmp_path):
    manifest = models.load_manifest()
    pin = manifest["comfyui"]["pinned_portable"]
    models.validate_download_url(pin["url"])
    assert models.manifest_path(tmp_path, pin["filename"], plain_name=True).parent == tmp_path
    for asset in manifest["assets"].values():
        models.validate_download_url(asset["url"])
        assert models.asset_dest(asset, tmp_path).resolve().is_relative_to(tmp_path.resolve())
        if asset.get("alt_filename"):
            assert models.asset_dest(asset, tmp_path, asset["alt_filename"]).parent.name == asset["subdir"]
    assert models.manifest_path(tmp_path, "nested/models").parts[-2:] == ("nested", "models")
    models.validate_download_url("https://github.com:443/file")


def test_logged_values_strip_controls_and_tokens(caplog):
    url = "https://github.com/a\x00b\x7fc\x85d\nforged?secret=token#fragment"
    with caplog.at_level("INFO", logger=models.log.name):
        models.log.info("downloading %s", models.redact_url(url))
    assert caplog.records[0].getMessage() == "downloading https://github.com/abcdforged"
    assert models._log_value("1\r\n\t\x1b\x9f2") == "12"


def test_trusted_download_url_uses_the_allowlisted_origin():
    assert models.trusted_download_url("https://huggingface.co/a/b.safetensors") == (
        "https://huggingface.co/a/b.safetensors", "huggingface.co")
    assert models.trusted_download_url("https://huggingface.co/a b/c") == ("https://huggingface.co/a%20b/c", "huggingface.co")
    assert models.trusted_download_url("https://huggingface.co/r/file%20name.bin")[0] == "https://huggingface.co/r/file%20name.bin"
    for bad in ("https://huggingface.co/a?download=1", "https://huggingface.co/a/../b", "https://huggingface.co/./b",
                "https://huggingface.co/a/%2e%2e/b", "https://huggingface.co/a/%2E/b"):
        with pytest.raises(ValueError):
            models.trusted_download_url(bad)
    assert models.trusted_download_url("https://github.com:443/r/x.7z") == ("https://github.com/r/x.7z", "github.com")
    with pytest.raises(ValueError):
        models.trusted_download_url("https://us.aws.cdn.hf.co/x")
