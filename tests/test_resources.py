#!/usr/bin/env python3
"""Regression tests for the GPU meter in Resources.qml.

Run them from anywhere, with nothing to install:

    python3 tests/test_resources.py -v

They need python3 plus sh, cat, head, tail, free, awk, df, tr and rm, which
every Arch/Omarchy system has. The widget tests also need quickshell; the live
tests need the Omarchy shell modules in /usr/share/omarchy/shell (and qmllint
for the lint test). Tests whose tools are missing are skipped.

- Poll script tests take the sh -c script verbatim from Resources.qml, point it
  at a fake /sys/class/drm and run it with a PATH holding only the tools above
  and, where a test wants one, a fake nvidia-smi.
- Widget tests load Resources.qml, changed only in that path, in a headless
  Quickshell through tests/harness.qml, call its parseGpu and record real
  polls. They use STUB qs.Commons and qs.Ui modules (see STUBS).
- Live tests run the unmodified script and widget, with the installed Omarchy
  modules, on this machine's GPU, and print what they read.

Quickshell runs with QT_QPA_PLATFORM=offscreen and its own HOME and XDG
directories inside a temporary directory, so it opens no window, never meets
the running shell and writes nothing outside that directory.
"""

import json
import math
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
WIDGET = TESTS.parent / "Resources.qml"
HARNESS = TESTS / "harness.qml"
OMARCHY_SHELL = Path("/usr/share/omarchy/shell")
DRM = "/sys/class/drm"
TOOLS = ["sh", "cat", "head", "tail", "free", "awk", "df", "tr", "rm"]
QUICKSHELL = shutil.which("quickshell")
QMLLINT = shutil.which("qmllint")
HAVE_OMARCHY = all((OMARCHY_SHELL / name / "qmldir").is_file() for name in ("Commons", "Ui"))

# STUBS, not Omarchy code: just enough of qs.Commons and qs.Ui for
# Resources.qml to load, so the widget tests need neither the Omarchy shell
# nor a theme. The live tests use the real modules.
STUBS = {
    "Commons/qmldir": "module qs.Commons\nsingleton Style 1.0 Style.qml\nsingleton Color 1.0 Color.qml\n",
    "Commons/Style.qml": "pragma Singleton\nimport QtQuick\nQtObject { function space(n) { return n } }\n",
    "Commons/Color.qml": 'pragma Singleton\nimport QtQuick\nQtObject {\n'
                         '  property color foreground: "white"\n  property color urgent: "red"\n}\n',
    "Ui/qmldir": "module qs.Ui\nBarWidget 1.0 BarWidget.qml\n",
    "Ui/BarWidget.qml": 'import QtQuick\nItem {\n  property QtObject bar: null\n'
                        '  property string moduleName: ""\n  property var settings: ({})\n'
                        '  readonly property int barSize: 26\n}\n',
}

# A fake nvidia-smi, for tests only. Its state is in files under STATE:
# "answers" holds one reading per line, GPU 0 first; with "fail" it prints that
# file and exits 9, like nvidia-smi without its driver; with "ignore-id" it
# lists every GPU even when asked for one. Each call is logged to "calls".
FAKE_NVIDIA_SMI = """#!/bin/sh
state=STATE
printf '%s\\n' "$*" >> "$state/calls"
if [ -e "$state/fail" ]; then cat "$state/fail"; exit 9; fi
id=all
[ -e "$state/ignore-id" ] || for arg; do case $arg in --id=*) id=${arg#--id=} ;; esac; done
n=0 found=
while IFS= read -r line; do
  if [ "$id" = all ] || [ "$id" = "$n" ]; then printf '%s\\n' "$line"; found=1; fi
  n=$((n + 1))
done < "$state/answers"
[ -n "$found" ] || { echo "No devices were found"; exit 6; }
"""


class Unreadable:
    """A gpu_busy_percent that exists but fails to read."""

    def __repr__(self):
        return "UNREADABLE"


UNREADABLE = Unreadable()

# What sysfs might hold instead of a reading: each must be skipped. None means
# no gpu_busy_percent at all.
BAD_SYSFS = ["", "N/A", "-1", "+5", "101", "1000", "45.5", "4 5", "45%", "1e2", "0x10",
             "45\n46", "99999999999999999999", None, UNREADABLE]

# parseGpu: accepted text and its value, and text it must turn down. None
# stands for a poll without a GPU: line.
GOOD = {"0": 0, "7": 7, "45": 45, "100": 100, "007": 7, " 7 ": 7, "45\n": 45, "\t12\r\n": 12}
BAD = ["", "   ", None, "N/A", "[N/A]", "[Not Supported]", "NaN", "Infinity", "-Infinity",
       "-1", "-0", "+5", "101", "1000", "45.5", "4.", ".5", "4 5", "37 88", "45%", "1e2",
       "0x10", "0b1", "45\n46", "45abc", "١٢", "99999999999999999999",
       "NVIDIA-SMI has failed because it couldn't communicate with the NVIDIA driver."]


def q(path):
    return shlex.quote(str(path))


def poll_script():
    """The script Resources.qml hands to sh -c, exactly as written there."""
    found = re.findall(r'command: \["sh", "-c", `(.*?)`\]', WIDGET.read_text(), re.S)
    if len(found) != 1:
        raise AssertionError("Resources.qml should run one sh -c template string")
    script = found[0]
    # A JS template string without ${, \ or ` is its text verbatim.
    if re.search(r"\$\{|\\|`", script) or script.count(DRM) != 1:
        raise AssertionError("the poll script must name %s once and hold no ${, \\ or `" % DRM)
    return script


def check_poll(test, run):
    """Check a finished run of the poll script; return the text after GPU:."""
    test.assertEqual(run.returncode, 0, run.stderr)
    test.assertEqual(run.stderr, "", "expected errors should be kept quiet")
    lines = run.stdout.split("\n")
    test.assertEqual(len(lines), 5, run.stdout)
    test.assertEqual(lines[4], "", run.stdout)
    cpu, mem, gpu, disk = lines[:4]
    test.assertRegex(cpu, r"^CPU:cpu( +[0-9]+)+$")
    test.assertRegex(mem, r"^MEM:[0-9]+ [0-9]+$")
    used, total = map(int, mem[4:].split())
    test.assertTrue(0 < used <= total, mem)
    test.assertTrue(gpu.startswith("GPU:"), gpu)
    test.assertRegex(disk, r"^DISK: *[0-9]+%$")
    return gpu[4:]


class Fixture:
    """A fake /sys/class/drm, an optional fake nvidia-smi, and a PATH with
    nothing else but the tools the poll script uses."""

    def __init__(self, root):
        self.root = root
        self.drm = root / "sys" / "class" / "drm"
        self.drm.mkdir(parents=True)
        self.bin = root / "bin"
        self.bin.mkdir()
        for tool in TOOLS:
            found = shutil.which(tool)
            if not found:
                raise unittest.SkipTest("needs " + tool)
            (self.bin / tool).symlink_to(found)
        self.nvidia_state = root / "nvidia"
        self.nvidia_state.mkdir()
        self.script = poll_script().replace(DRM, q(self.drm))

    def card(self, name, busy=None, vendor="0x1002"):
        """Add <drm>/<name>/device with a vendor file and, unless busy is None,
        a gpu_busy_percent holding busy. Returns the gpu_busy_percent path."""
        device = self.drm / name / "device"
        device.mkdir(parents=True)
        if vendor is not None:
            (device / "vendor").write_text(vendor + "\n")
        busy_file = device / "gpu_busy_percent"
        if busy is UNREADABLE:
            busy_file.mkdir()  # reading a directory fails
        elif busy is not None:
            busy_file.write_text(busy + "\n")
        return busy_file

    def nvidia(self, *readings, fail=None, ignore_id=False):
        """Put the fake nvidia-smi on PATH, answering with one reading per GPU."""
        smi = self.bin / "nvidia-smi"
        smi.write_text(FAKE_NVIDIA_SMI.replace("STATE", q(self.nvidia_state)))
        smi.chmod(0o755)
        (self.nvidia_state / "answers").write_text("".join(r + "\n" for r in readings))
        (self.nvidia_state / "fail").unlink(missing_ok=True)
        (self.nvidia_state / "ignore-id").unlink(missing_ok=True)
        if fail is not None:
            (self.nvidia_state / "fail").write_text(fail)
        if ignore_id:
            (self.nvidia_state / "ignore-id").touch()

    def nvidia_calls(self):
        calls = self.nvidia_state / "calls"
        return calls.read_text().splitlines() if calls.exists() else []

    def poll(self, test):
        run = subprocess.run(["sh", "-c", self.script], cwd=self.root, capture_output=True,
                             text=True, timeout=30, env={"PATH": str(self.bin), "LANG": "C.UTF-8"})
        return check_poll(test, run)


def like_this_machine(fx, busy):
    """A Radeon 880M/890M as card1, no card0, connectors and a render node
    beside it. Unlike real sysfs, the connectors and the render node here look
    like AMD GPUs too, so only skipping them by name gets the right answer."""
    busy_file = fx.card("card1", busy)
    for name in ("card1-DP-1", "card1-DP-2", "card1-eDP-1", "card1-Writeback-1", "renderD128"):
        fx.card(name, "77")
    return busy_file


def run_harness(tmp, job, modules, path):
    """Run tests/harness.qml on `job` in a headless Quickshell and return its
    result. `modules` is STUBS, or None for the installed Omarchy modules."""
    config = tmp / "config"
    config.mkdir()
    shutil.copy(HARNESS, config / "shell.qml")
    if modules is None:
        for name in ("Commons", "Ui"):
            (config / name).symlink_to(OMARCHY_SHELL / name)
    else:
        for name, text in modules.items():
            (config / name).parent.mkdir(exist_ok=True)
            (config / name).write_text(text)
    env = {"PATH": path, "LANG": "C.UTF-8", "NO_COLOR": "1", "QT_QPA_PLATFORM": "offscreen",
           "RESOURCES_TEST": json.dumps(job)}
    for name in ("HOME", "XDG_RUNTIME_DIR", "XDG_CACHE_HOME", "XDG_CONFIG_HOME",
                 "XDG_DATA_HOME", "XDG_STATE_HOME"):
        env[name] = str(tmp / name.lower())
        os.mkdir(env[name], 0o700)

    shell = subprocess.Popen([QUICKSHELL, "--no-color", "-p", str(config)], cwd=tmp, env=env,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    watchdog = threading.Timer(60, shell.kill)
    watchdog.start()
    log, result = [], None
    try:
        for line in shell.stdout:
            log.append(line)
            if "RESULT " in line:
                result = json.loads(line.split("RESULT ", 1)[1])
                break
    finally:
        watchdog.cancel()
        shell.terminate()
        try:
            shell.wait(10)
        except subprocess.TimeoutExpired:
            shell.kill()
            shell.wait()
        shell.stdout.close()

    output = "".join(log)
    if result is None:
        raise AssertionError("the harness gave no result:\n" + output)
    if "error" in result:
        raise AssertionError(result["error"] + "\n" + output)
    warnings = re.findall(r".*\b(?:Resources|shell)\.qml:\d+.*", output)
    if warnings:
        raise AssertionError("QML warnings:\n" + "\n".join(warnings))
    return result


def gpu_states(result):
    return [(poll["gpuAvailable"], poll["gpu"]) for poll in result["polls"]]


def check_metrics(test, result):
    """CPU, RAM and disk keep working in every recorded poll."""
    for poll in result["polls"]:
        test.assertGreater(poll["cpuSample"], 0, poll)
        test.assertTrue(0 <= poll["cpu"] <= 100, poll)
        test.assertTrue(0 < poll["ram"] <= 100, poll)
        test.assertTrue(0 < poll["disk"] <= 100, poll)


class PollScriptTest(unittest.TestCase):
    """The poll script from Resources.qml, run against fake GPUs."""

    def fixture(self):
        tmp = tempfile.TemporaryDirectory(prefix="resources-test-")
        self.addCleanup(tmp.cleanup)
        return Fixture(Path(tmp.name))

    def setUp(self):
        self.fx = self.fixture()

    def test_amd_card1_without_card0(self):
        like_this_machine(self.fx, "45")
        self.assertEqual(self.fx.poll(self), "45")

    def test_connectors_and_render_nodes_are_not_cards(self):
        like_this_machine(self.fx, None)
        self.fx.card("card", "77")
        self.fx.card("cardX", "77")
        self.assertEqual(self.fx.poll(self), "")

    def test_only_amd_cards_count(self):
        self.fx.card("card0", "11", vendor="0x8086")  # Intel
        self.fx.card("card1", "22", vendor="0x10de")  # NVIDIA
        self.fx.card("card2", "33", vendor=None)
        self.fx.card("card3", "44")
        self.assertEqual(self.fx.poll(self), "44")

    def test_zero_and_100_are_readings(self):
        self.fx.nvidia("50")
        busy = self.fx.card("card0", "0")
        self.assertEqual(self.fx.poll(self), "0")
        busy.write_text("100\n")
        self.assertEqual(self.fx.poll(self), "100")
        self.assertEqual(self.fx.nvidia_calls(), [], "an AMD reading needs no nvidia-smi")

    def test_first_amd_card_in_glob_order(self):
        self.fx.card("card0", "20")
        self.fx.card("card1", "30")
        self.assertEqual(self.fx.poll(self), "20")
        fx = self.fixture()
        fx.card("card2", "2")
        fx.card("card10", "10")  # sorts before card2
        self.assertEqual(fx.poll(self), "10")

    def test_bad_amd_readings_are_skipped(self):
        for bad in BAD_SYSFS:
            with self.subTest(busy=bad):
                fx = self.fixture()
                fx.card("card0", bad)
                self.assertEqual(fx.poll(self), "")
                fx.nvidia("50")
                self.assertEqual(fx.poll(self), "50")
                fx.card("card1", "30")
                self.assertEqual(fx.poll(self), "30")

    def test_nvidia_gpu_0_without_an_amd_reading(self):
        self.fx.card("card0", vendor="0x10de")  # NVIDIA's own card: no gpu_busy_percent
        self.fx.nvidia("37", "88")
        self.assertEqual(self.fx.poll(self), "37")
        calls = self.fx.nvidia_calls()
        self.assertEqual(len(calls), 1)
        self.assertIn("--id=0", calls[0].split())

    def test_nvidia_only(self):
        self.fx.nvidia("45")
        self.assertEqual(self.fx.poll(self), "45")

    def test_nvidia_answer_is_passed_on_whole_on_one_line(self):
        # The script leaves judging it to parseGpu (see WidgetTest).
        self.fx.nvidia("[N/A]")
        self.assertEqual(self.fx.poll(self), "[N/A]")
        self.fx.nvidia("55", "66", ignore_id=True)
        self.assertEqual(self.fx.poll(self), "55 66")

    def test_failing_nvidia_smi_gives_no_reading(self):
        self.fx.nvidia(fail="NVIDIA-SMI has failed because it couldn't communicate with the NVIDIA driver.\n")
        self.assertEqual(self.fx.poll(self), "")
        self.fx.nvidia(fail="45\n")
        self.assertEqual(self.fx.poll(self), "")

    def test_no_gpu(self):
        self.assertEqual(self.fx.poll(self), "")
        self.fx.drm.rmdir()
        self.assertEqual(self.fx.poll(self), "")

    def test_reads_afresh_every_poll(self):
        busy = like_this_machine(self.fx, "45")
        self.assertEqual(self.fx.poll(self), "45")
        busy.unlink()
        self.assertEqual(self.fx.poll(self), "")
        self.fx.nvidia("12")
        self.assertEqual(self.fx.poll(self), "12")
        busy.write_text("60\n")
        self.assertEqual(self.fx.poll(self), "60")


@unittest.skipUnless(QUICKSHELL, "needs quickshell")
class WidgetTest(unittest.TestCase):
    """Resources.qml in a headless Quickshell, with STUB modules."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="resources-test-")
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.fx = Fixture(self.tmp / "fixture")

    def run_widget(self, **job):
        """Load the widget, its poll script pointed at the fixture, and run job."""
        widget = self.tmp / "plugin" / "Resources.qml"
        widget.parent.mkdir()
        widget.write_text(WIDGET.read_text().replace(poll_script(), self.fx.script))
        job["widget"] = widget.as_uri()
        result = run_harness(self.tmp, job, STUBS, str(self.fx.bin))
        self.assertEqual(result["command"], ["sh", "-c", self.fx.script])
        return result

    def test_parse_gpu(self):
        # Each text follows one with the opposite verdict, so a stale value
        # left behind would show.
        inputs, expected = [], []
        for text, value in GOOD.items():
            inputs += ["N/A", text]
            expected += [[False, 0], [True, value]]
        for text in BAD:
            inputs += ["45", text]
            expected += [[True, 45], [False, 0]]
        result = self.run_widget(parse=inputs)
        self.assertEqual(len(result["parse"]), len(inputs))
        for text, got, want in zip(inputs, result["parse"], expected):
            with self.subTest(text=text):
                self.assertEqual(got, want)

    def test_amd_polls(self):
        busy = like_this_machine(self.fx, "45")
        result = self.run_widget(polls=3, drive=True, steps=[
            "rm " + q(busy),
            "printf '60\\n' > " + q(busy),
        ])
        self.assertEqual(gpu_states(result), [(True, 45), (False, 0), (True, 60)])
        check_metrics(self, result)

    def test_nvidia_polls(self):
        self.fx.card("card0", vendor="0x8086")  # an Intel GPU gives no reading
        self.fx.nvidia("37", "88")
        state = self.fx.nvidia_state
        result = self.run_widget(polls=5, drive=True, steps=[
            "printf 'NVIDIA-SMI has failed\\n' > " + q(state / "fail"),
            "rm " + q(state / "fail") + " && printf '[N/A]\\n' > " + q(state / "answers"),
            ": > " + q(state / "ignore-id") + " && printf '55\\n66\\n' > " + q(state / "answers"),
            "rm " + q(state / "ignore-id") + " && printf '0\\n' > " + q(state / "answers"),
        ])
        self.assertEqual(gpu_states(result), [(True, 37), (False, 0), (False, 0), (False, 0), (True, 0)])
        check_metrics(self, result)

    def test_polls_without_a_gpu(self):
        result = self.run_widget(polls=2, drive=True)
        self.assertEqual(gpu_states(result), [(False, 0), (False, 0)])
        check_metrics(self, result)


@unittest.skipUnless(QUICKSHELL and HAVE_OMARCHY, "needs quickshell and the Omarchy shell modules")
class LiveTest(unittest.TestCase):
    """The unmodified script and widget on this machine, with Omarchy's modules."""

    def test_widget_on_this_machine(self):
        run = subprocess.run(["sh", "-c", poll_script()], capture_output=True, text=True, timeout=30)
        reading = check_poll(self, run)
        has_gpu = re.fullmatch(r"[0-9]+", reading) is not None and int(reading) <= 100

        tmp = tempfile.TemporaryDirectory(prefix="resources-test-")
        self.addCleanup(tmp.cleanup)
        result = run_harness(Path(tmp.name), {"widget": WIDGET.as_uri(), "polls": 3}, None,
                             os.environ.get("PATH", "/usr/bin:/bin"))
        print("\n  poll script GPU: %r" % reading)
        for poll in result["polls"]:
            print("  widget: CPU %(cpu).1f%%  RAM %(ram).1f%%  Disk %(disk).0f%%  "
                  "gpuAvailable=%(gpuAvailable)s  gpuPercent=%(gpu)s" % poll)

        self.assertEqual(result["command"], ["sh", "-c", poll_script()])
        self.assertEqual(len(result["polls"]), 3)
        check_metrics(self, result)
        samples = [poll["cpuSample"] for poll in result["polls"]]
        self.assertEqual(samples, sorted(set(samples)), "every poll should take a new CPU sample")
        for poll in result["polls"]:
            self.assertEqual(poll["gpuAvailable"], has_gpu, poll)
            self.assertTrue(math.isfinite(poll["gpu"]) and 0 <= poll["gpu"] <= 100, poll)
            if not has_gpu:
                self.assertEqual(poll["gpu"], 0, poll)

    @unittest.skipUnless(QMLLINT, "needs qmllint")
    def test_qmllint(self):
        tmp = tempfile.TemporaryDirectory(prefix="resources-test-")
        self.addCleanup(tmp.cleanup)
        imports = Path(tmp.name) / "qs"
        imports.mkdir()
        for name in ("Commons", "Ui"):
            (imports / name).symlink_to(OMARCHY_SHELL / name)
        for qml in (WIDGET, HARNESS):
            with self.subTest(qml=qml.name):
                run = subprocess.run([QMLLINT, "-I", tmp.name, str(qml)], capture_output=True,
                                     text=True, timeout=60)
                self.assertEqual((run.returncode, run.stdout + run.stderr), (0, ""))


if __name__ == "__main__":
    unittest.main()
