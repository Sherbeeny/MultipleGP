#!/usr/bin/env python3
"""Clone builder for MultipleGP. Driven ENTIRELY by clones.json - edit that one
file (add/rename a clone) and commit; the workflow picks it up.

Subcommands (run by .github/workflows/build-clones.yml):
  detect        gate job: find newest upstream (mentalblank) version, decide build
  one           build a single clone APK (env CLONE_LETTER, UPSTREAM_VERSION)
  verify        assert one built APK's package/authorities/spoof meta-data
  cross-verify  uniqueness across clones + identical spoof identity family-wide
  release       create the GitHub release + SHA256SUMS + meta.json, bump state
"""
import json, os, re, subprocess, sys, hashlib, urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CFG = json.load(open(os.path.join(ROOT, "clones.json")))
OUT = os.path.join(ROOT, "out")
WORK = os.path.join(ROOT, "work")

# morphe-cli redirects to morphe-desktop; v1.18.1 verified 2026-10-22
MORPHE_CLI = "https://github.com/MorpheApp/morphe-desktop/releases/download/v1.18.1/morphe-desktop-1.18.1-all.jar"
# apktool latest verified 2026-10-22
APKTOOL = "https://github.com/iBotPeaches/Apktool/releases/download/v3.0.3/apktool_3.0.3.jar"


def sh(*args, **kw):
    print("+", " ".join(str(a) for a in args), flush=True)
    subprocess.run([str(a) for a in args], check=True, **kw)


def gh_api(path):
    req = urllib.request.Request("https://api.github.com" + path)
    req.add_header("Accept", "application/vnd.github+json")
    tok = os.environ.get("GH_TOKEN")
    if tok:
        req.add_header("Authorization", "Bearer " + tok)
    return json.load(urllib.request.urlopen(req))


def clone(letter):
    for c in CFG["clones"]:
        if c["letter"] == letter:
            return c
    sys.exit("no clone with letter %r in clones.json" % letter)


def version_of(rel):
    """Longest dotted version token anywhere in the upstream release (tag, title,
    asset names, body). mentalblank tags are bare build numbers ("44"); the real
    Photos version lives in the asset name/body, e.g. 7.92.0.977185651."""
    hay = [rel.get("tag_name") or "", rel.get("name") or "", rel.get("body") or ""]
    hay += [a.get("name", "") for a in rel.get("assets", [])]
    best = None
    for s in hay:
        for m in re.finditer(r"\d+\.\d+(?:\.\d+)+", s):
            if best is None or len(m.group(0)) > len(best):
                best = m.group(0)
    if best is None:
        sys.exit("could not parse version from upstream release: %r" % rel.get("tag_name"))
    return best


def spoof_identity(manifest):
    """{meta-data android:name: android:value} for SPOOFED_PACKAGE_* entries,
    tolerant of attribute order."""
    out = {}
    for tag in re.findall(r"<meta-data[^>]*/?>", manifest):
        nm = re.search(r'android:name="([^"]+)"', tag)
        val = re.search(r'android:value="([^"]*)"', tag)
        if nm and "SPOOFED_PACKAGE" in nm.group(1):
            out[nm.group(1)] = val.group(1) if val else ""
    return out


def _heartbeat():
    """Monthly activity commit so GitHub never auto-disables the schedule
    (60d inactivity policy), even when there is nothing to build."""
    if os.environ.get("GITHUB_EVENT_NAME") != "schedule":
        return
    import datetime
    open("heartbeat.txt", "w").write(datetime.datetime.now(datetime.timezone.utc).isoformat() + "\n")
    sh("git", "config", "user.name", "github-actions[bot]")
    sh("git", "config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com")
    sh("git", "add", "heartbeat.txt")
    rc = subprocess.run(["git", "commit", "-m", "heartbeat: detect ran, nothing to build"], check=False).returncode
    if rc == 0:
        # detached HEAD in Actions: push explicitly to main, and fail loudly
        sh("git", "push", "origin", "HEAD:main")


def cmd_detect():
    out = os.environ.get("GITHUB_OUTPUT") and open(os.environ["GITHUB_OUTPUT"], "a")
    force = os.environ.get("FORCE", "false") == "true"
    if os.environ.get("GITHUB_EVENT_NAME") == "schedule":
        import datetime, zoneinfo
        if datetime.datetime.now(zoneinfo.ZoneInfo("Africa/Cairo")).day != 21:
            print("not the 21st in Cairo - scheduled slot skipped")
            if out: out.write("build=false\nmatrix=[]\nversion=\n")
            return
    supported = CFG["bundle"].get("supported_versions", [])
    pin = os.environ.get("PIN_VERSION", "").strip()
    if pin:
        # Manual dispatch pinned a version: it must be bundle-supported.
        ver = pin
        print("manual version pin:", ver)
    else:
        rel = gh_api("/repos/%s/releases/latest" % CFG["upstream"]["track_repo"])
        ver = version_of(rel)
    if supported and ver not in supported:
        print("upstream %s NOT in bundle supported_versions %s - skipping (no broken build, no release)"
              % (ver, supported))
        if out: out.write("build=false\nmatrix=[]\nversion=%s\n" % ver)
        _heartbeat()
        return
    tag = "photos-" + ver
    repo = os.environ["GITHUB_REPOSITORY"]
    try:
        gh_api("/repos/%s/releases/tags/%s" % (repo, tag))
        exists = True
    except Exception:
        exists = False
    build = force or not exists
    print("upstream latest = %s (%s); our release %s exists=%s force=%s -> build=%s"
          % (ver, rel.get("tag_name"), tag, exists, force, build))
    if not build:
        _heartbeat()
    matrix = [{"letter": c["letter"], "package": c["package"], "app_name": c["app_name"]} for c in CFG["clones"]]
    if out:
        out.write("build=%s\nversion=%s\nmatrix=%s\n" % (str(build).lower(), ver, json.dumps(matrix)))


def dl(url, dest):
    if not os.path.exists(dest):
        print("download", url)
        urllib.request.urlretrieve(url, dest)


def cmd_one():
    letter = os.environ["CLONE_LETTER"]
    ver = os.environ["UPSTREAM_VERSION"]
    c = clone(letter)
    os.makedirs(OUT, exist_ok=True)
    os.makedirs(WORK, exist_ok=True)
    stock = os.path.join(WORK, "stock-%s.apk" % ver)
    if not os.path.exists(stock):
        last = None
        for tpl in CFG["upstream"]["stock_apk_sources"]:
            try:
                dl(tpl.format(version=ver), stock)
                break
            except Exception as e:
                last = e
                print("source failed:", tpl, e)
        else:
            sys.exit("no stock APK source worked: %s" % last)
    dl(MORPHE_CLI, os.path.join(WORK, "morphe.jar"))
    bundle_dir = os.path.join(WORK, "bundle")
    if not os.path.exists(bundle_dir):
        sh("git", "clone", "--depth", "1", "--branch", CFG["bundle"]["patches_ref"],
           "https://github.com/%s" % CFG["bundle"]["patches_repo"], bundle_dir)
        sh("cp", "-r", os.path.join(ROOT, "custom-patches", "."), bundle_dir)
        sh("./gradlew", ":patches:build", cwd=bundle_dir)
    mpp = None
    for base, _, files in os.walk(bundle_dir):
        for f in files:
            if f.endswith(".mpp"):
                mpp = os.path.join(base, f)
    assert mpp, "no .mpp bundle built"
    patched = os.path.join(WORK, "patched-%s.apk" % letter)
    sh("java", "-jar", os.path.join(WORK, "morphe.jar"), "patch", stock,
       "-p", mpp, "-e", "Custom package name", "-OpackageName=" + c["package"],
       "-o", patched)
    dl(APKTOOL, os.path.join(WORK, "apktool.jar"))
    dec = os.path.join(WORK, "dec-%s" % letter)
    sh("java", "-jar", os.path.join(WORK, "apktool.jar"), "d", "-f", "-o", dec, patched)
    for base, _, files in os.walk(os.path.join(dec, "res")):
        for f in files:
            if f == "strings.xml":
                p = os.path.join(base, f)
                s = open(p, encoding="utf-8").read()
                s2 = re.sub(r'(<string name="app_name">)[^<]*', r"\g<1>" + c["app_name"], s)
                if s2 != s:
                    open(p, "w", encoding="utf-8").write(s2)
    final = os.path.join(OUT, "%s-%s.apk" % (c["app_name"], ver))
    sh("java", "-jar", os.path.join(WORK, "apktool.jar"), "b", "-o", final, dec)
    ks = os.path.join(WORK, "family.keystore")
    if not os.path.exists(ks):
        import base64
        b64 = os.environ.get("SIGNING_KEYSTORE_B64")
        if not b64:
            sys.exit("SIGNING_KEYSTORE_B64 secret missing (key custody not set up yet)")
        open(ks, "wb").write(base64.b64decode(b64))
    sh("zipalign", "-p", "4", final, final + ".aligned")
    os.replace(final + ".aligned", final)
    sh("apksigner", "sign", "--ks", ks, "--ks-key-alias", os.environ.get("SIGNING_KEY_ALIAS", "family"),
       "--ks-pass", "env:SIGNING_STORE_PASSWORD", "--key-pass", "env:SIGNING_KEY_PASSWORD", final)
    print("built", final)


def manifest_of(apk, letter):
    dec = os.path.join(WORK, "verify-%s" % letter)
    if not os.path.exists(dec):
        sh("java", "-jar", os.path.join(WORK, "apktool.jar"), "d", "-f", "-s", "-o", dec, apk)
    return open(os.path.join(dec, "AndroidManifest.xml"), encoding="utf-8").read()


def cmd_verify():
    letter = os.environ["CLONE_LETTER"]
    ver = os.environ["UPSTREAM_VERSION"]
    c = clone(letter)
    apk = os.path.join(OUT, "%s-%s.apk" % (c["app_name"], ver))
    m = manifest_of(apk, letter)
    pkg = re.search(r'package="([^"]+)"', m).group(1)
    assert pkg == c["package"], "package %r != config %r" % (pkg, c["package"])
    vn = re.search(r'android:versionName="([^"]+)"', m)
    assert vn and vn.group(1) == ver, "versionName %r != upstream %s" % (vn and vn.group(1), ver)
    auths = set(re.findall(r'android:authorities="([^"]+)"', m))
    for a in auths:
        assert a == c["package"] or a.startswith(c["package"] + "."), \
            "foreign authority %r in %s" % (a, letter)
    for bad in ("app.revanced.android.apps.photos",
                "com.google.android.libraries.photos.api.mars",
                "app.morphe.android.apps.photos"):
        assert bad not in m, "residual upstream identity %s in %s" % (bad, letter)
    sid = spoof_identity(m)
    assert any("SPOOFED_PACKAGE_NAME" in k for k in sid), "SPOOFED_PACKAGE_NAME meta-data missing"
    assert any("SPOOFED_PACKAGE_SIGNATURE" in k for k in sid), "SPOOFED_PACKAGE_SIGNATURE meta-data missing"
    print("verify OK", letter, pkg, len(auths), "authorities, spoof meta-data present")


def cmd_cross_verify():
    os.makedirs(WORK, exist_ok=True)
    dl(APKTOOL, os.path.join(WORK, "apktool.jar"))
    seen_pkg, seen_auth, mars_prefixes, spoof_ids, certs = set(), set(), set(), set(), set()
    for c in CFG["clones"]:
        for f in os.listdir(OUT):
            if f.startswith("%s-" % c["app_name"]) and f.endswith(".apk"):
                m = manifest_of(os.path.join(OUT, f), c["letter"])
                pkg = re.search(r'package="([^"]+)"', m).group(1)
                assert pkg not in seen_pkg, "duplicate package " + pkg
                seen_pkg.add(pkg)
                for a in re.findall(r'android:authorities="([^"]+)"', m):
                    assert a not in seen_auth, "authority collision " + a
                    seen_auth.add(a)
                mp = re.search(r'android:authorities="([^"]*api\.mars[^"]*)"', m)
                assert mp, "no MARS authority in " + c["letter"]
                mars_prefixes.add(mp.group(1).split(".api.mars")[0])
                sid = spoof_identity(m)
                assert sid, "spoof meta-data missing in " + c["letter"]
                spoof_ids.add(tuple(sorted(sid.items())))
                r = subprocess.run(["apksigner", "verify", "--print-certs",
                                    os.path.join(OUT, f)], capture_output=True, text=True, check=True)
                dig = tuple(sorted(l.split(":", 1)[1].strip() for l in r.stdout.splitlines()
                                   if "certificate SHA-256 digest" in l))
                assert dig, "no signing cert found in " + f
                certs.add(dig)
    # MARS prefixes must DIFFER per clone (they are package-scoped) ...
    assert len(mars_prefixes) == len(seen_pkg), "MARS prefixes not unique per clone"
    # ... while the spoofed identity is byte-identical across the whole family
    assert len(spoof_ids) == 1, "spoof identity differs across clones: %r" % (spoof_ids,)
    # ... and every APK is signed by the SAME certificate (updates install cleanly)
    assert len(certs) == 1, "signing certs differ across clones"
    print("cross-verify OK:", len(seen_pkg), "unique packages,", len(seen_auth),
          "unique authorities, one shared spoof identity")


def cmd_release():
    ver = os.environ["UPSTREAM_VERSION"]
    tag = "photos-" + ver
    sums, assets = [], []
    for base, _, files in os.walk(OUT):
        for f in sorted(files):
            if f.endswith(".apk"):
                p = os.path.join(base, f)
                assets.append(p)
                sums.append("%s  %s" % (hashlib.sha256(open(p, "rb").read()).hexdigest(), f))
    assert assets, "no APKs to release"
    if os.environ.get("FORCE", "false") == "true":
        subprocess.run(["gh", "release", "delete", tag, "--yes"], check=False)  # force re-release
    open("SHA256SUMS", "w").write("\n".join(sums) + "\n")
    os.makedirs(WORK, exist_ok=True)
    dl(MORPHE_CLI, os.path.join(WORK, "morphe.jar"))   # pinned URLs; fetched here
    dl(APKTOOL, os.path.join(WORK, "apktool.jar"))     # so the hashes are real
    tools = {}
    for name in ("morphe.jar", "apktool.jar"):
        p = os.path.join(WORK, name)
        if os.path.exists(p):
            tools[name] = hashlib.sha256(open(p, "rb").read()).hexdigest()
    assert tools, "tool jars missing - meta.json tool_sha256 would be empty"
    meta = {"upstream": CFG["upstream"]["track_repo"], "upstream_version": ver,
            "bundle": CFG["bundle"], "clones": CFG["clones"],
            "files": [os.path.basename(a) for a in assets], "tool_sha256": tools}
    open("meta.json", "w").write(json.dumps(meta, indent=2) + "\n")
    sh("gh", "release", "create", tag, *assets, "SHA256SUMS", "meta.json",
       "--title", "Photos clones %s" % ver,
       "--notes", "Built from %s @ %s. One APK per clone in clones.json; point Obtainium at this repo and match your variant by filename (e.g. PhotosA-*.apk)."
                  % (CFG["upstream"]["track_repo"], ver))
    # keepalive + state: a monthly commit keeps the schedule alive (GitHub disables
    # schedules after 60d of no repo activity)
    open("state.json", "w").write(json.dumps({"last_built": ver, "tag": tag}) + "\n")
    sh("git", "config", "user.name", "github-actions[bot]")
    sh("git", "config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com")
    sh("git", "add", "state.json")
    rc = subprocess.run(["git", "commit", "-m", "state: built %s" % tag], check=False).returncode
    if rc == 0:
        # detached HEAD in Actions: push explicitly to main
        sh("git", "push", "origin", "HEAD:main")


CMD = {"detect": cmd_detect, "one": cmd_one, "verify": cmd_verify,
       "cross-verify": cmd_cross_verify, "release": cmd_release}
if __name__ == "__main__":
    CMD[sys.argv[1]]()
