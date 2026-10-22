import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import build_clones as bc

MANIFEST = """<?xml version="1.0" encoding="utf-8"?>
<manifest xmlns:android="http://schemas.android.com/apk/res/android" package="app.gclone.w">
    <application android:label="@string/photos_theme_google_photos" android:name=".PhotosRoot">
        <activity android:label="@string/home_title" android:name=".home.HomeActivity" android:exported="true">
            <intent-filter>
                <action android:name="android.intent.action.MAIN" />
                <category android:name="android.intent.category.LAUNCHER" />
            </intent-filter>
        </activity>
        <activity android:label="@string/settings_title" android:name=".settings.SettingsActivity" />
        <activity-alias android:name=".alias.LauncherAlias" android:targetActivity=".home.HomeActivity" android:exported="true">
            <intent-filter>
                <action android:name="android.intent.action.MAIN" />
                <category android:name="android.intent.category.LAUNCHER" />
            </intent-filter>
        </activity-alias>
        <activity android:name=".share.ShareActivity">
            <intent-filter>
                <action android:name="android.intent.action.SEND" />
            </intent-filter>
        </activity>
    </application>
</manifest>
"""


class LauncherLabelTests(unittest.TestCase):
    def _patched(self, label="PhotosW"):
        import tempfile
        d = tempfile.mkdtemp()
        p = os.path.join(d, "AndroidManifest.xml")
        open(p, "w", encoding="utf-8").write(MANIFEST)
        bc.patch_launcher_labels(p, label)
        return open(p, encoding="utf-8").read()

    def test_application_label_set_literal(self):
        out = self._patched()
        self.assertIn('<application android:label="PhotosW"', out.replace("\n", " ").replace("    ", " "))
        self.assertNotIn('@string/photos_theme_google_photos', out.split("<activity")[0])

    def test_launcher_activity_label_set_literal(self):
        out = self._patched()
        self.assertNotIn("@string/home_title", out)

    def test_launcher_alias_gets_label_inserted(self):
        out = self._patched()
        alias = out.split("<activity-alias")[1].split(">")[0]
        self.assertIn('android:label="PhotosW"', alias)

    def test_non_launcher_components_untouched(self):
        out = self._patched()
        self.assertIn("@string/settings_title", out)
        share = out.split(".share.ShareActivity")[0]
        self.assertNotIn('android:label="PhotosW"', share.split("<activity")[-1])

    def test_package_and_other_attrs_preserved(self):
        out = self._patched()
        self.assertIn('package="app.gclone.w"', out)
        self.assertIn('.PhotosRoot', out)

    def test_check_launcher_labels_passes_after_patch(self):
        bc.check_launcher_labels(self._patched(), "PhotosW")

    def test_check_launcher_labels_rejects_unpatched(self):
        with self.assertRaises(AssertionError):
            bc.check_launcher_labels(MANIFEST, "PhotosW")


if __name__ == "__main__":
    unittest.main()
