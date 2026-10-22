package app.morphe.patches.googlephotos.misc.gms

import app.morphe.patcher.patch.bytecodePatch
import app.morphe.patcher.patch.stringOption
import app.morphe.patches.shared.misc.gms.PackageNameConfig

@Suppress("unused")
val customPackageNamePatch = bytecodePatch(
    name = "Custom package name",
    description = "Renames the app and its provider authorities to a unique package name so this " +
        "clone can install alongside other copies of Google Photos. " +
        "Each clone must use a different value, e.g. app.morphe.android.apps.photos.c1",
    default = false,
) {
    val packageName by stringOption(
        key = "packageName",
        default = null,
        title = "Package name",
        description = "Unique package name for this clone (lowercase, dot-separated). " +
            "Example: app.morphe.android.apps.photos.c1",
        required = true,
    ) {
        it != null && it.matches(Regex("^[a-z][a-z0-9_]*(\\.[a-z][a-z0-9_]*)+\$"))
    }

    execute {
        packageName?.let { name ->
            PackageNameConfig.customPackageName = name
        }
    }
}
