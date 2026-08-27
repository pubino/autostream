#!/usr/bin/env python3
"""
Static checks over the AutoSignDisplay source and asset catalog.

    python3 scripts/check-source-patterns.py

These were Swift test suites (FieldEditingPatternTests, AppIconAssetTests, and the
source-reading half of AccessibilityPatternTests). They read the repository through
`#filePath`, which works locally because the simulator shares the developer's
filesystem — and fails in Xcode Cloud, where the test bundle runs in an environment
that no longer has the checkout:

    The folder "AutoSignDisplay" doesn't exist.
    /Volumes/workspace/repository/AutoSignDisplay

They were never really unit tests. Nothing here needs a simulator, a build product,
or a running app; every check reads text and JSON off disk. As a script they run
wherever the source is: locally via scripts/run-tests.sh, and in CI via
ci_scripts/ci_post_clone.sh, which executes on the build machine with the repository
checked out.

No third-party dependencies — PNG dimensions are read from the IHDR chunk directly,
because Pillow is not present on Apple's build machines.

Exits non-zero on the first failing category, listing every failure found.
"""

from __future__ import annotations

import json
import re
import struct
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
APP = REPO_ROOT / "AutoSignDisplay"
BRAND = APP / "Assets.xcassets" / "App Icon & Top Shelf Image.brandassets"

failures: list[str] = []


def fail(check: str, message: str) -> None:
    failures.append(f"{check}: {message}")


# ---------- source helpers ----------

def code(filename: str) -> str:
    """Comment-stripped source, so checks match real code and not the comments that
    describe the very patterns being enforced."""
    text = (APP / filename).read_text(encoding="utf-8")
    out = []
    for line in text.split("\n"):
        index = line.find("//")
        out.append(line if index == -1 else line[:index])
    return "\n".join(out)


def app_sources() -> list[tuple[str, str]]:
    return [(p.name, code(p.name)) for p in sorted(APP.glob("*.swift"))]


def body(header: str, source: str) -> str | None:
    """Text inside the braces of the declaration introduced by `header`, by brace
    matching. Comments are already stripped, so none can throw off the count."""
    start = source.find(header)
    if start == -1:
        return None
    open_brace = source.find("{", start)
    if open_brace == -1:
        return None
    depth = 0
    for i in range(open_brace, len(source)):
        if source[i] == "{":
            depth += 1
        elif source[i] == "}":
            depth -= 1
            if depth == 0:
                return source[open_brace + 1:i]
    return None


def region(needle: str, source: str, length: int = 400) -> str | None:
    index = source.find(needle)
    return None if index == -1 else source[index + len(needle):index + len(needle) + length]


# ---------- presentation rules ----------
# Background: editing a preset's Name or URL used to leave the field stuck in tvOS's
# compact editing presentation whenever the user backed out of the keyboard. Every
# broken field lived inside a presented modal; the working one was reached by a
# navigation push.

def check_presentation() -> None:
    content = code("ContentView.swift")
    for needle, why in [
        ("NavigationLink", "ContentView must reach Settings and Manage Stream Presets with NavigationLink"),
        ("ChannelPresetsView(", "ContentView should construct ChannelPresetsView( as a push destination"),
        ("SettingsGateView(", "ContentView should reach Settings through SettingsGateView("),
    ]:
        if needle not in content:
            fail("presentation", why)

    if "SettingsView(" not in code("SettingsView.swift"):
        fail("presentation", "SettingsGateView should construct SettingsView once unlocked")

    for name, source in app_sources():
        for line in source.split("\n"):
            if "fullScreenCover" in line and "$showPlayer" not in line:
                fail("presentation", f"{name} presents a fullScreenCover that is not the video player: {line.strip()}")
        if ".sheet(" in source:
            fail("presentation", f"{name} uses .sheet; tvOS truncates it and text fields inside one stick")
        if "focused ?" in source:
            fail("presentation", f"{name} colors a control from a @FocusState flag; tvOS owns text field contrast")

    presets = code("ChannelPresetsView.swift")
    if "LabeledTextField(" not in presets:
        fail("presentation", "Preset rows should edit through LabeledTextField")
    if "@FocusState" in presets:
        fail("presentation", "ChannelPresetsView declares @FocusState; the fields need no focus plumbing")

    # The watchdog must outlive the main view disappearing. ContentView disappears when
    # the fullscreen player is presented — the moment self-healing matters most — and
    # also when Settings or Manage Presets is pushed.
    for line in content.split("\n"):
        if "onDisappear" in line:
            following = region("onDisappear", content, 200) or ""
            if "stopRetryTimer" in following:
                fail("presentation",
                     "ContentView stops the retry timer in onDisappear. That fires when the "
                     "fullscreen player is presented, killing the watchdog exactly when it is "
                     "needed. Backgrounding is handled by scenePhase; an explicit stop by "
                     "stopPlayback().")
            break

    labeled = body("struct LabeledTextField: View {", code("SettingsView.swift"))
    if labeled is None:
        fail("presentation", "LabeledTextField not found in SettingsView.swift")
    else:
        for banned in ("@FocusState", ".focused("):
            if banned in labeled:
                fail("presentation", f"LabeledTextField contains {banned}; it must stay plain")


# ---------- accessibility rules ----------
# Hide a visual affordance, and the state it showed has to be re-exposed on the
# focusable container. Forgetting the second half happened twice in two files.

def check_accessibility() -> None:
    settings = code("SettingsView.swift")

    row_label = body("struct RowLabel: View {", settings)
    if row_label is None:
        fail("accessibility", "RowLabel not found")
    elif ".accessibilityHidden(true)" not in row_label:
        fail("accessibility",
             "RowLabel's trailing value is not hidden; SwiftUI merges it into the Button's "
             "label and the wrapper's .accessibilityValue then repeats it")

    for row in ("struct SettingToggleRow: View {", "struct SettingCycleRow: View {"):
        declaration = body(row, settings)
        if declaration is None:
            fail("accessibility", f"{row} not found")
        elif ".accessibilityValue(" not in declaration:
            fail("accessibility", f"{row} draws its value through RowLabel, which hides it; "
                                  "without .accessibilityValue the value is inaudible")

    pin_row = region('title: "Settings PIN"', settings)
    if pin_row is None:
        fail("accessibility", "Settings PIN status row not found")
    elif ".accessibilityValue(" not in pin_row:
        fail("accessibility", "The Settings PIN status row uses RowLabel outside a Button, "
                              "so nothing speaks Set/Not set")

    # Every branch of the PIN editor that sets a visible error must announce it: the
    # screen neither pops nor moves focus on failure, so silence is the only signal.
    evaluate = body("private func evaluate() {", settings)
    if evaluate is None:
        fail("accessibility", "SettingsPINEditorView.evaluate() not found")
    else:
        lines = evaluate.split("\n")
        assignments = 0
        for i, line in enumerate(lines):
            if 'errorMessage = "' not in line:
                continue
            assignments += 1
            branch = [line]
            for following in lines[i + 1:]:
                stripped = following.strip()
                if stripped.startswith("}"):
                    break
                branch.append(following)
                if stripped.startswith("return"):
                    break
            if "Announcement(" not in "\n".join(branch):
                fail("accessibility", f"evaluate() sets a visible error without announcing it: {line.strip()}")
        if assignments == 0:
            fail("accessibility", "expected evaluate() to still set visible error messages")
        if "PIN set" not in evaluate:
            fail("accessibility", "evaluate() should still announce success")

    presets = code("ChannelPresetsView.swift")
    group = body("private struct PresetGroup: View {", presets)
    if group is None:
        fail("accessibility", "PresetGroup not found")
    else:
        if ".accessibilityHidden(true)" not in group:
            fail("accessibility", "PresetGroup's SELECTED/PLAYING marker should stay out of accessibility")
        # Checking the property exists is not enough: hardcoding the modifier leaves
        # it in place, unused, and selection goes silent again.
        if ".accessibilityLabel(accessibilityLabel)" not in group:
            fail("accessibility", "PresetGroup does not apply its state-dependent accessibilityLabel")
        label = body("private var accessibilityLabel: String {", group)
        if label is None:
            fail("accessibility", "PresetGroup has no accessibilityLabel property")
        else:
            if "isSelected" not in label:
                fail("accessibility", "PresetGroup's accessibilityLabel does not vary with isSelected")
            if "playing" not in label or "selected" not in label:
                fail("accessibility", "PresetGroup's label should distinguish selected from playing")

    message = body("private func deleteConfirmationMessage(for index: Int) -> String {", presets)
    if message is None:
        fail("accessibility", "deleteConfirmationMessage not found")
    else:
        if "spokenDescriptor" not in message:
            fail("accessibility", "deleteConfirmationMessage should use ChannelPreset.spokenDescriptor; "
                                  "it is read aloud before a destructive confirmation")
        if "preset.url" in message:
            fail("accessibility", "deleteConfirmationMessage should not fall back to the raw URL")

    play = region(".disabled(viewModel.streamURL.isEmpty)", code("ContentView.swift"))
    if play is None:
        fail("accessibility", "Play Stream's disabled modifier not found")
    elif ".accessibilityHint(" not in play:
        fail("accessibility", 'Play Stream is disabled with no hint; VoiceOver says only "dimmed"')


# ---------- project configuration ----------
# Every defect found while setting up the second distribution identity was a project
# setting, not code: Xcode's target duplication kept the *old* bundle identifier in the
# new target's Debug configuration, left neither target with a compilation condition, and
# put the new scheme in xcuserdata where Xcode Cloud cannot see it. A second test target
# would have caught none of those. These checks catch all three, and run in CI.

APP_IDENTITIES = {
    "AutoSignDisplay": "edu.princeton.autosigndisplay",
    "AutoStreamDisplay": "edu.princeton.autostreamdisplay",
}
PRIVATE_IDENTITY = "AutoSignDisplay"


def build_configurations() -> list[dict]:
    """Every XCBuildConfiguration as {name, settings}, parsed from project.pbxproj."""
    text = (REPO_ROOT / "AutoSignDisplay.xcodeproj" / "project.pbxproj").read_text()
    out = []
    for match in re.finditer(r'isa = XCBuildConfiguration;(.*?)name = (\w+);', text, re.S):
        block, name = match.group(1), match.group(2)
        settings = dict(re.findall(r'^\t+([A-Z_a-z]+) = ([^;]+);', block, re.M))
        out.append({"config": name, "settings": settings})
    return out


def check_project() -> None:
    configs = build_configurations()

    # Group the app-target configurations by display name, which is what distinguishes
    # the two identities in the project file.
    by_identity: dict[str, list[dict]] = {}
    for entry in configs:
        display = entry["settings"].get("INFOPLIST_KEY_CFBundleDisplayName")
        if display in APP_IDENTITIES:
            by_identity.setdefault(display, []).append(entry)

    for identity, expected_bundle in APP_IDENTITIES.items():
        found = by_identity.get(identity, [])
        if len(found) != 2:
            fail("project", f"expected Debug and Release configurations for {identity}, "
                            f"found {len(found)}")
            continue

        for entry in found:
            actual = entry["settings"].get("PRODUCT_BUNDLE_IDENTIFIER")
            if actual != expected_bundle:
                fail("project",
                     f"{identity} {entry['config']} has bundle identifier {actual!r}, "
                     f"expected {expected_bundle!r}. Xcode's target editor applies this to "
                     f"the selected configuration only, so a duplicated target can keep the "
                     f"original identifier in Debug — and the two apps would then replace "
                     f"each other on a device instead of installing side by side.")

            conditions = entry["settings"].get("SWIFT_ACTIVE_COMPILATION_CONDITIONS", "")
            has_private = "PRIVATE_DISTRIBUTION" in conditions
            should = identity == PRIVATE_IDENTITY
            if has_private != should:
                fail("project",
                     f"{identity} {entry['config']} "
                     f"{'is missing' if should else 'must not define'} PRIVATE_DISTRIBUTION. "
                     f"That flag selects which default channel list is compiled in, so the "
                     f"wrong value ships one identity's channels inside the other.")

    # The os_log subsystem must be derived, not written out. A literal files one
    # identity's logs under the other's name — which is how the public build ended up
    # logging as edu.princeton.autosigndisplay, making the documented retrieval commands
    # return nothing for it.
    logger_source = code("Logger.swift")
    if "Bundle.main.bundleIdentifier" not in logger_source:
        fail("project", "Logger.swift should derive its os_log subsystem from "
                        "Bundle.main.bundleIdentifier. A hardcoded subsystem sends one "
                        "distribution identity's diagnostics to the other's name.")
    for identity_bundle in APP_IDENTITIES.values():
        if f'subsystem: "{identity_bundle}"' in logger_source:
            fail("project", f"Logger.swift hardcodes the subsystem as {identity_bundle!r}.")

    # Xcode Cloud cannot build a scheme that is not shared, and target duplication leaves
    # the new one in xcuserdata. The failure looks like a workflow finding nothing to do.
    shared = REPO_ROOT / "AutoSignDisplay.xcodeproj" / "xcshareddata" / "xcschemes"
    for identity in APP_IDENTITIES:
        if not (shared / f"{identity}.xcscheme").is_file():
            fail("project", f"{identity}.xcscheme is not shared. Xcode Cloud cannot see a "
                            f"scheme outside xcshareddata, so this identity cannot be built "
                            f"by CI at all.")

    # Both targets build their products into one directory, so a script that names the
    # bundle after the *project* rather than the *scheme* installs whichever app was
    # built last under that filename. run.sh did exactly this: it built
    # AutoStreamDisplay.app, installed a stale AutoSignDisplay.app, and then launched the
    # public bundle identifier successfully, because an older copy was already there.
    # Nothing failed, and the app on screen was not the app just built.
    for script in sorted((REPO_ROOT / "scripts").rglob("*.sh")):
        # Comment-stripped, like the Swift checks above. The comment in run.sh explaining
        # this very bug quotes the offending string, and matching that would make the
        # guard fire on the fix.
        text = "\n".join(line for line in script.read_text(encoding="utf-8").splitlines()
                          if not line.lstrip().startswith("#"))
        if "$PROJECT_NAME.app" in text or "${PROJECT_NAME}.app" in text:
            fail("project", f"{script.relative_to(REPO_ROOT)} names an app bundle after "
                            f"PROJECT_NAME. Use APP_BUNDLE_NAME from select_app, which "
                            f"reads FULL_PRODUCT_NAME for the chosen scheme — otherwise "
                            f"--app is ignored and the other identity gets installed.")


# ---------- icon asset catalog ----------
# Build 3 of 1.0 was rejected on upload with ITMS-90709 for a missing 2x background
# layer. Xcode builds and the simulator renders fine without it; nothing surfaces
# until a full archive, upload and Apple's processing have completed.

def png_size(path: Path) -> tuple[int, int]:
    with path.open("rb") as handle:
        header = handle.read(24)
    if len(header) < 24:
        raise ValueError(f"{path} is not a readable PNG")
    return struct.unpack(">II", header[16:24])


def manifest(directory: Path) -> list[dict]:
    return json.loads((directory / "Contents.json").read_text(encoding="utf-8")).get("images", [])


LAYERS = ("Back", "Middle", "Front")


def check_icons() -> None:
    if not BRAND.is_dir():
        fail("icons", f"brand assets not found at {BRAND}")
        return

    for layer in LAYERS:
        directory = BRAND / "App Icon.imagestack" / f"{layer}.imagestacklayer" / "Content.imageset"
        scales = sorted(entry["scale"] for entry in manifest(directory))
        if scales != ["1x", "2x"]:
            fail("icons", f"home-screen App Icon '{layer}' declares {scales}; tvOS needs 1x and 2x "
                          "(ITMS-90709, and Apple names only the first missing layer)")
        for entry in manifest(directory):
            expected = (800, 480) if entry["scale"] == "2x" else (400, 240)
            actual = png_size(directory / entry["filename"])
            if actual != expected:
                fail("icons", f"{layer}/{entry['filename']} is {actual[0]}x{actual[1]}, expected "
                              f"{expected[0]}x{expected[1]}")

    for layer in LAYERS:
        directory = BRAND / "App Icon - App Store.imagestack" / f"{layer}.imagestacklayer" / "Content.imageset"
        images = manifest(directory)
        # Deliberate: the App Store icon has no 2x variant in the tvOS specification.
        if [entry["scale"] for entry in images] != ["1x"]:
            fail("icons", f"App Store icon '{layer}' should declare 1x only")
        elif png_size(directory / images[0]["filename"]) != (1280, 768):
            fail("icons", f"App Store icon '{layer}' should be 1280x768")

    for name, base in (("Top Shelf Image", (1920, 720)), ("Top Shelf Image Wide", (2320, 720))):
        directory = BRAND / f"{name}.imageset"
        images = manifest(directory)
        if sorted(entry["scale"] for entry in images) != ["1x", "2x"]:
            fail("icons", f"{name} should provide both scales")
        for entry in images:
            expected = (base[0] * 2, base[1] * 2) if entry["scale"] == "2x" else base
            actual = png_size(directory / entry["filename"])
            if actual != expected:
                fail("icons", f"{name} {entry['scale']} is {actual[0]}x{actual[1]}, expected "
                              f"{expected[0]}x{expected[1]}")

    # A manifest entry without its file fails at upload, not at build time.
    checked = 0
    for contents in BRAND.rglob("Contents.json"):
        for entry in manifest(contents.parent):
            # An entry with no filename is an unassigned slot — legal, and nothing to
            # verify. Xcode's tvOS template leaves one in an imagestacklayer.
            if "filename" not in entry:
                continue
            if not (contents.parent / entry["filename"]).is_file():
                fail("icons", f"{contents.parent.name} declares {entry['filename']} but the file is missing")
            checked += 1
    if checked < 13:
        fail("icons", f"expected at least 13 declared images, saw {checked}")


# ---------- run ----------

def main() -> int:
    for name, check in (("presentation", check_presentation),
                        ("accessibility", check_accessibility),
                        ("project", check_project),
                        ("icons", check_icons)):
        try:
            check()
        except Exception as error:  # a check that cannot run is a failure, not a pass
            fail(name, f"check raised {type(error).__name__}: {error}")

    if failures:
        print(f"source pattern checks: {len(failures)} failure(s)\n", file=sys.stderr)
        for failure in failures:
            print(f"  {failure}", file=sys.stderr)
        return 1

    print("source pattern checks: all passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
