import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Commons as Commons
import qs.Ui

// Compact CPU / RAM / GPU / disk usage meters, polled every 2s. Exact values
// are in the hover tooltip; click opens Activity. CPU% needs a
// delta between two /proc/stat samples, so the first tick after startup
// reads 0% until the second sample lands.
BarWidget {
  id: root
  moduleName: "io.github.yesm1ke.resources"

  property real cpuPercent: 0
  property real ramPercent: 0
  property real gpuPercent: 0
  property bool gpuAvailable: false
  property real diskPercent: 0

  property real prevIdle: -1
  property real prevTotal: -1

  // A meter turns urgent at or above its threshold. CPU spikes constantly, so
  // it has to stay above for cpuHotSamples polls in a row before it counts.
  readonly property int cpuHotAt: 80
  readonly property int ramHotAt: 50
  readonly property int gpuHotAt: 50
  readonly property int diskHotAt: 70
  readonly property int cpuHotSamples: 3
  property int cpuHotStreak: 0

  readonly property color meterColor: bar ? bar.barForeground : Commons.Color.foreground
  readonly property color urgentColor: bar ? bar.urgent : Commons.Color.urgent

  readonly property var meters: [
    { name: "CPU", value: cpuPercent, hot: cpuHotStreak >= cpuHotSamples, shown: true },
    { name: "RAM", value: ramPercent, hot: ramPercent >= ramHotAt, shown: true },
    { name: "GPU", value: gpuPercent, hot: gpuPercent >= gpuHotAt, shown: gpuAvailable },
    { name: "Disk", value: diskPercent, hot: diskPercent >= diskHotAt, shown: true }
  ]

  implicitWidth: row.implicitWidth + Style.space(16)
  implicitHeight: barSize

  function refresh() {
    if (!statsProc.running) statsProc.running = true
  }

  function parseCpu(line) {
    var parts = line.trim().split(/\s+/).slice(1).map(Number)
    var idle = parts[3] + (parts[4] || 0)
    var total = 0
    for (var i = 0; i < parts.length; i++) total += parts[i]

    if (root.prevTotal >= 0) {
      var dTotal = total - root.prevTotal
      var dIdle = idle - root.prevIdle
      if (dTotal > 0) root.cpuPercent = Math.max(0, Math.min(100, 100 * (1 - dIdle / dTotal)))
      root.cpuHotStreak = root.cpuPercent >= root.cpuHotAt ? root.cpuHotStreak + 1 : 0
    }
    root.prevTotal = total
    root.prevIdle = idle
  }

  // GPU: holds one utilisation reading or nothing. Only a whole number from 0
  // to 100 counts; anything else hides the meter and zeroes it, so it never
  // shows a stale or made-up value.
  function parseGpu(line) {
    var text = (line || "").trim()
    var valid = /^[0-9]+$/.test(text) && Number(text) <= 100
    root.gpuAvailable = valid
    root.gpuPercent = valid ? Number(text) : 0
  }

  Process {
    id: statsProc
    // Each line is tagged (CPU:/MEM:/GPU:/DISK:) rather than read by position,
    // so a missing GPU reading can't shift later fields. GPU: is the first AMD
    // card, in glob order, whose gpu_busy_percent holds a whole percentage
    // (connectors like card1-DP-1 are not cards), else NVIDIA GPU 0's answer
    // on one line for parseGpu to judge, else empty. This is a JS template
    // string: keep ${, \ and backticks out of the script.
    command: ["sh", "-c", `
      gpu() {
        drm=/sys/class/drm
        for card in "$drm"/card*; do
          case $card in "$drm"/card | "$drm"/card*[!0-9]*) continue ;; esac
          [ "$(cat "$card/device/vendor")" = 0x1002 ] || continue
          busy=$(cat "$card/device/gpu_busy_percent") || continue
          case $busy in '' | *[!0-9]*) continue ;; esac
          [ "$busy" -le 100 ] && echo "$busy" && return
        done
        busy=$(nvidia-smi --id=0 --query-gpu=utilization.gpu --format=csv,noheader,nounits) &&
          printf %s "$busy" | tr '[:space:]' ' '
      } 2>/dev/null
      echo "CPU:$(head -1 /proc/stat)"
      echo "MEM:$(free -b | awk 'NR==2{print $3, $2}')"
      echo "GPU:$(gpu)"
      echo "DISK:$(df --output=pcent / | tail -1)"
    `]
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        var fields = {}
        text.split("\n").forEach(function(line) {
          var i = line.indexOf(":")
          if (i > 0) fields[line.slice(0, i)] = line.slice(i + 1)
        })

        if (fields.CPU) root.parseCpu(fields.CPU)

        if (fields.MEM) {
          var mem = fields.MEM.trim().split(/\s+/).map(Number)
          if (mem.length === 2 && mem[1] > 0) root.ramPercent = 100 * mem[0] / mem[1]
        }

        root.parseGpu(fields.GPU)

        if (fields.DISK) {
          var diskLine = fields.DISK.trim().replace("%", "")
          if (diskLine !== "") root.diskPercent = Number(diskLine)
        }
      }
    }
  }

  Timer {
    interval: 2000
    running: true
    repeat: true
    triggeredOnStart: true
    onTriggered: root.refresh()
  }

  // Same command the stock "Activity" binding (SUPER + CTRL + T) runs.
  function openActivity() {
    if (root.bar) root.bar.run("omarchy-launch-tui btop")
  }

  // The bar only shows a tooltip for a target that reports itself hovered.
  readonly property bool tooltipHovered: visible && opacity > 0 && mouseArea.containsMouse
  readonly property string tooltipText: meters.filter(function(m) { return m.shown }).map(function(m) {
    return m.name + " " + m.value.toFixed(0) + "%"
  }).join("  ·  ")

  MouseArea {
    id: mouseArea
    anchors.fill: parent
    hoverEnabled: true
    cursorShape: Qt.PointingHandCursor
    onClicked: root.openActivity()
    onEntered: if (root.bar) root.bar.showTooltip(root, root.tooltipText)
    onExited: if (root.bar) root.bar.hideTooltip(root)
  }

  Row {
    id: row
    anchors.centerIn: parent
    spacing: Style.space(3)

    Repeater {
      model: root.meters

      // One meter: a dim track with a fill rising from the bottom.
      Rectangle {
        id: track
        required property var modelData
        readonly property color tint: modelData.hot ? root.urgentColor : root.meterColor

        visible: modelData.shown
        width: Style.space(4)
        height: Style.space(12)
        radius: 1
        color: Qt.rgba(tint.r, tint.g, tint.b, 0.25)

        Rectangle {
          anchors.left: parent.left
          anchors.right: parent.right
          anchors.bottom: parent.bottom
          height: Math.max(1, Math.round(parent.height * track.modelData.value / 100))
          radius: 1
          color: track.tint
        }
      }
    }
  }
}
