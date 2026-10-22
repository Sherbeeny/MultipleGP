# Building side-by-side Google Photos clones

Goal: install several patched Google Photos copies on one device at the same time,
each with its own package name, its own provider authorities, and its own Google
account (via MicroG-RE), with no manual profile switching.

## Why a new patch was needed

The upstream bundle (Akash-Sriram/morphe-google-photos) already contains all the
machinery to rename the app (`PackageNameConfig.customPackageName` in the shared
GmsCore support patch drives the manifest package, every `android:authorities`
value including the MARS authority, dex string references, and the prime method).
But no shipped patch ever *sets* that config, so stock bundles always produce the
same package (`app.morphe.android.apps.photos`) and collide with each other on
install (`INSTALL_FAILED_CONFLICTING_PROVIDER` / same package).

`CustomPackageNamePatch.kt` closes that gap: it exposes one required option,
`packageName`, and assigns it to `PackageNameConfig.customPackageName` during the
execute phase, before any finalize block rewrites the manifest or dex.

## Layout (overlay on Akash-Sriram/morphe-google-photos v1.15.4)

```
custom-patches/
  patches/src/main/kotlin/app/morphe/patches/googlephotos/misc/gms/CustomPackageNamePatch.kt
```

Copy `custom-patches/*` over a clean checkout of the patches repo, then build the
bundle as usual (`./gradlew :patches:build`). The new patch appears as
"Custom package name" with a required `packageName` option
(validated as a lowercase dot-separated package id).

## Building one clone (Morphe CLI)

```
java -jar morphe.jar patch <base.apk> \
  -p patches-custom.mpp \
  -e "Custom package name" -OpackageName=app.gclone.a \
  -o PhotosA-7.97.0.997578476.apk
```

- Base APK: Google Photos 7.97.0.997578476 (universal, nodpi) - the only version
  the v1.15.4 bundle targets. Source: APKMirror.
- Each clone needs its own packageName value. The live family is Letters, not
  numbers: PhotosA/PhotosS/PhotosW/PhotosX -> app.gclone.a/.s/.w/.x, all driven
  by clones.json at the repo root (edit that ONE file to add or rename a copy).
  Never reuse a package value; once installed, a package name is fixed forever.
- Sign every clone with the same keystore so future updates install cleanly.
- MicroG-RE (one install) serves all clones: it keys auth tokens per
  package+signature and reads the spoof metadata the bundle injects.

## Verified example output (clone A)

- `package="app.gclone.a"` (was app.morphe.android.apps.photos.c1 in the original c1/c2/c3 verification)
- every provider authority under the same scope, e.g.
  `app.gclone.a.api.mars`
- spoof metadata `app.revanced.android.gms.SPOOFED_PACKAGE_NAME` /
  `SPOOFED_PACKAGE_SIGNATURE` present
- versionName 7.97.0.997578476 / versionCode 52547203

## Known behavior notes

- "DCIM & backup control" is enabled by default in the upstream bundle: camera
  folders are NOT auto-backed-up until enabled per folder in each clone's
  settings. Disable the patch at build time (`-d "DCIM & backup control"`) if
  that is not wanted.
- Do not enable "Official package name" together with "Custom package name";
  official wins and the clone collides with the stock app.
