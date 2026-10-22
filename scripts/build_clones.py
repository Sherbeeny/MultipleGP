#!/usr/bin/env python3
"""Clone builder for MultipleGP. Driven ENTIRELY by clones.json - edit that one
file (add/rename a clone, pin a version) and commit; the workflow picks it up.

APK source (Sherbo, topic 76, 2026-10-22): ONLY the release assets of the
tracked upstream repo (mentalblank/GPhotos-Revanced). Upstream ships ONE
universal, already-De-Vanced-patched APK per release (Pixel-feature spoof incl.
unlimited storage, DCIM backup control, account-persistence fix), so this
builder does NOT patch with a bundle - it clones that APK: rename package +
authorities + app label, then sign with the family keystore.

Subcommands (run by .github/workflows/build-clones.yml):
  detect        gate job: find newest upstream release, decide build
  one           build a single clone APK (env CLONE_LETTER, UPSTREAM_VERSION, ASSET_URL)
  verify        assert one built APK's package/authorities/spoof meta-data/a12 support
  cross-verify  uniqueness across clones + identical spoof identity family-wide
  release       create the GitHub release + SHA256SUMS + meta.json, bump state.json
"""
import json, os, re, subprocess, sys, hashlib, urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CFG = json.load(open(os.path.join(ROOT, "clones.json")))
OUT = os.path.join(ROOT, "out")
WORK = os.path.join(ROOT, "work")
STATE = os.path.join(ROOT, "state.json")

# Package identities used by upstream builds; every occurrence is renamed to
# the clone's package. ("app.revanced..." survives upstream as their MARS
# authority and is renamed too; "app.revanced.android.gms" is microG's own
# package referenced by meta-data VALUES and must NOT be touched - the exact
# strings below do not match it.)
UPSTREAM_PKG_PREFIXES = ("app.morphe.android.apps.photos",
                         "app.revanced.android.apps.photos")

# apktool latest verified 2026-10-22
APKTOOL = "https://github.com/iBotPeaches/Apktool/releases/download/v3.0.3/apktool_3.0.3.jar"

# Android 12 = API 31. The focused version must install on a12 (Sherbo's order);
# verify() fails loudly if upstream ever ships a higher minSdk, and an older
# version can then be pinned via clones.json upstream.pinned_version.
A12_MAX_MINSDK = 31


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


def asset_of(rel, ver):
    """The release's APK asset for version ver (prefer the universal '-all')."""
    cands = [a for a in rel.get("assets", [])
             if a.get("name", "").endswith(".apk") and ver in a.get("name", "")]
    if not cands:
        sys.exit("upstream release %r has no .apk asset containing version %s"
                 % (rel.get("tag_name"), ver))
    cands.sort(key=lambda a: ("-all" not in a["name"], a["name"]))
    return cands[0]


def find_upstream_release(pin):
    """(rel, version, asset). pin empty -> latest release; otherwise the newest
    release whose parsed Photos version equals pin."""
    repo = CFG["upstream"]["track_repo"]
    if not pin:
        rel = gh_api("/repos/%s/releases/latest" % repo)
        ver = version_of(rel)
        return rel, ver, asset_of(rel, ver)
    for page in range(1, 6):
        rels = gh_api("/repos/%s/releases?per_page=100&page=%d" % (repo, page))
        if not rels:
            break
        for rel in rels:
            try:
                if version_of(rel) == pin:
                    return rel, pin, asset_of(rel, pin)
            except SystemExit:
                continue
    sys.exit("no upstream release of %s carries Photos version %s" % (repo, pin))


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


def read_state():
    try:
        return json.load(open(STATE))
    except Exception:
        return {}


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
            if out: out.write("build=false\nmatrix=[]\nversion=\nasset_url=\nupstream_tag=\n")
            return
    pin = (os.environ.get("PIN_VERSION", "").strip()
           or str(CFG["upstream"].get("pinned_version") or "").strip())
    rel, ver, asset = find_upstream_release(pin)
    if pin:
        print("version pin:", ver, "(dispatch input or clones.json upstream.pinned_version)")
    tag = rel.get("tag_name") or ""
    st = read_state()
    already = (not force and not pin and st.get("last_upstream_tag")
               and st.get("last_upstream_tag") == tag)
    repo = os.environ["GITHUB_REPOSITORY"]
    try:
        gh_api("/repos/%s/releases/tags/%s" % (repo, "photos-" + ver))
        release_exists = True
    except Exception:
        release_exists = False
    build = force or bool(pin) or not (already or (release_exists and st.get("last_upstream_version") == ver))
    print("upstream latest = %s (tag %s); state last_tag=%r last_ver=%r; our release exists=%s force=%s -> build=%s"
          % (ver, tag, st.get("last_upstream_tag"), st.get("last_upstream_version"),
             release_exists, force, build))
    if not build:
        _heartbeat()
    matrix = [{"letter": c["letter"], "package": c["package"], "app_name": c["app_name"]} for c in CFG["clones"]]
    if out:
        out.write("build=%s\nversion=%s\nmatrix=%s\nasset_url=%s\nupstream_tag=%s\n"
                  % (str(build).lower(), ver, json.dumps(matrix),
                     asset["browser_download_url"], tag))


def dl(url, dest):
    if not os.path.exists(dest):
        print("download", url)
        urllib.request.urlretrieve(url, dest)


def sdk_tool(name):
    """build-tools binaries (apksigner, zipalign) are NOT on PATH on GitHub
    runners; they live in the preinstalled Android SDK under build-tools/<ver>/.
    Resolve dynamically (no setup action needed)."""
    import shutil, glob
    w = shutil.which(name)
    if w:
        return w
    home = os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT") or ""
    cands = sorted(glob.glob(os.path.join(home, "build-tools", "*", name)))
    assert cands, "%s not found on PATH or under %s/build-tools" % (name, home)
    return cands[-1]


def replace_pkg_in_tree(root, new_pkg):
    """Rename every upstream package occurrence to the clone package across all
    text files of a decoded APK tree (manifest, smali, xml, properties)."""
    hits = 0
    for base, _, files in os.walk(root):
        for f in files:
            p = os.path.join(base, f)
            try:
                data = open(p, "rb").read()
            except OSError:
                continue
            if b"\0" in data[:4096]:
                continue  # binary
            try:
                s = data.decode("utf-8")
            except UnicodeDecodeError:
                continue
            s2 = s
            for pre in UPSTREAM_PKG_PREFIXES:
                s2 = s2.replace(pre, new_pkg)
            if s2 != s:
                open(p, "w", encoding="utf-8").write(s2)
                hits += 1
    print("package renamed in", hits, "files ->", new_pkg)
    return hits


def cmd_one():
    letter = os.environ["CLONE_LETTER"]
    ver = os.environ["UPSTREAM_VERSION"]
    c = clone(letter)
    os.makedirs(OUT, exist_ok=True)
    os.makedirs(WORK, exist_ok=True)
    stock = os.path.join(WORK, "upstream-%s.apk" % ver)
    if not os.path.exists(stock):
        url = os.environ.get("ASSET_URL", "").strip()
        if not url:
            _, _, asset = find_upstream_release(ver)
            url = asset["browser_download_url"]
        dl(url, stock)
    digest = hashlib.sha256(open(stock, "rb").read()).hexdigest()
    open(os.path.join(WORK, "upstream-%s.sha256" % ver), "w").write(digest + "\n")
    print("upstream apk sha256:", digest)
    dl(APKTOOL, os.path.join(WORK, "apktool.jar"))
    dec = os.path.join(WORK, "dec-%s" % letter)
    sh("java", "-jar", os.path.join(WORK, "apktool.jar"), "d", "-f", "-o", dec, stock)
    replace_pkg_in_tree(dec, c["package"])
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
    sh(sdk_tool("zipalign"), "-p", "4", final, final + ".aligned")
    os.replace(final + ".aligned", final)
    sh(sdk_tool("apksigner"), "sign", "--ks", ks, "--ks-key-alias", os.environ.get("SIGNING_KEY_ALIAS", "family"),
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
    ms = re.search(r'android:minSdkVersion="(\d+)"', m)
    assert ms and int(ms.group(1)) <= A12_MAX_MINSDK, \
        "minSdk %s breaks Android 12 support (pin an older version in clones.json)" % (ms and ms.group(1))
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
    print("verify OK", letter, pkg, len(auths), "authorities, spoof meta-data present, a12-supported")


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
                r = subprocess.run([sdk_tool("apksigner"), "verify", "--print-certs",
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
    upstream_tag = os.environ.get("UPSTREAM_TAG", "")
    tag = "photos-" + ver
    sums, assets = [], []
    for base, _, files in os.walk(OUT):
        for f in sorted(files):
            if f.endswith(".apk"):
                p = os.path.join(base, f)
                assets.append(p)
                sums.append("%s  %s" % (hashlib.sha256(open(p, "rb").read()).hexdigest(), f))
    assert assets, "no APKs to release"
    # idempotent re-release: a re-run (force, pin, or same-version new upstream
    # build) replaces the existing tag instead of failing on it
    subprocess.run(["gh", "release", "delete", tag, "--yes"], check=False)
    open("SHA256SUMS", "w").write("\n".join(sums) + "\n")
    os.makedirs(WORK, exist_ok=True)
    dl(APKTOOL, os.path.join(WORK, "apktool.jar"))     # so the hash is real
    tools = {}
    p = os.path.join(WORK, "apktool.jar")
    if os.path.exists(p):
        tools["apktool.jar"] = hashlib.sha256(open(p, "rb").read()).hexdigest()
    assert tools, "tool jars missing - meta.json tool_sha256 would be empty"
    meta = {"upstream": CFG["upstream"]["track_repo"], "upstream_version": ver,
            "upstream_tag": upstream_tag, "clones": CFG["clones"],
            "files": [os.path.basename(a) for a in assets], "tool_sha256": tools}
    open("meta.json", "w").write(json.dumps(meta, indent=2) + "\n")
    sh("gh", "release", "create", tag, *assets, "SHA256SUMS", "meta.json",
       "--title", "Photos clones %s" % ver,
       "--notes", "Built from %s release %s (version %s). One APK per clone in clones.json; point Obtainium at this repo and match your variant by filename (e.g. PhotosA-*.apk)."
                  % (CFG["upstream"]["track_repo"], upstream_tag or "?", ver))
    # keepalive + state: a monthly commit keeps the schedule alive (GitHub disables
    # schedules after 60d of no repo activity)
    import datetime
    state = {"last_upstream_tag": upstream_tag, "last_upstream_version": ver,
             "built_at": datetime.datetime.now(datetime.timezone.utc).isoformat()}
    open(STATE, "w").write(json.dumps(state, indent=2) + "\n")
    sh("git", "config", "user.name", "github-actions[bot]")
    sh("git", "config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com")
    sh("git", "add", "state.json")
    rc = subprocess.run(["git", "commit", "-m", "state: built %s (upstream %s)" % (tag, upstream_tag or "?")],
                        check=False).returncode
    if rc == 0:
        # detached HEAD in Actions: push explicitly to main
        sh("git", "push", "origin", "HEAD:main")


CMD = {"detect": cmd_detect, "one": cmd_one, "verify": cmd_verify,
       "cross-verify": cmd_cross_verify, "release": cmd_release}
if __name__ == "__main__":
    CMD[sys.argv[1]]()
