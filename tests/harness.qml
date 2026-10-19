// Test harness for tests/test_resources.py, not part of the plugin. The test
// copies it into a throwaway Quickshell config as shell.qml and runs it
// headless with a JSON job in RESOURCES_TEST:
//   widget  file URL of the Resources.qml to load
//   parse   texts to hand to the widget's parseGpu, one after another
//   polls   how many polls to record
//   drive   stop the widget's own 2 s timer and start each poll from here, so
//           a step never races a poll; without it polls keep real timing
//   steps   with drive, a shell command to run before poll 2, 3, ...
// It logs one line, "RESULT <json>", and the test then stops it.
import QtQuick
import Quickshell
import Quickshell.Io

ShellRoot {
  id: harness

  readonly property var job: JSON.parse(Quickshell.env("RESOURCES_TEST"))
  property var widget: null
  property var poller: null
  property var ticker: null
  property var result: ({ command: [], parse: [], polls: [] })

  function report(error) {
    if (error) result.error = String(error)
    console.log("RESULT " + JSON.stringify(result))
  }

  function start(item) {
    widget = item
    for (var i = 0; i < item.resources.length; i++) {
      var object = item.resources[i]
      if (object.command !== undefined) poller = object
      else if (object.triggeredOnStart !== undefined) ticker = object
    }
    if (!poller || !ticker) return report("the widget has no poll Process or Timer")
    for (var c = 0; c < poller.command.length; c++) result.command.push(poller.command[c])

    var inputs = job.parse || []
    for (var p = 0; p < inputs.length; p++) {
      item.parseGpu(inputs[p])
      result.parse.push([item.gpuAvailable, item.gpuPercent])
    }

    if (!job.polls) return report()
    poller.stdout.streamFinished.connect(function() { Qt.callLater(harness.polled) })
    if (job.drive) {
      ticker.running = false
      item.refresh()
    }
  }

  // Runs after the widget's own onStreamFinished has taken in a poll.
  function polled() {
    result.polls.push({
      cpuSample: widget.prevTotal,
      cpu: widget.cpuPercent,
      ram: widget.ramPercent,
      disk: widget.diskPercent,
      gpuAvailable: widget.gpuAvailable,
      gpu: widget.gpuPercent
    })
    if (result.polls.length >= job.polls) report()
    else if (job.drive) nextPoll()
  }

  // Once the last poll has exited, run the next step, if any, then poll.
  function nextPoll() {
    if (poller.running) return settle.start()
    var step = (job.steps || [])[result.polls.length - 1]
    if (!step) return widget.refresh()
    stepper.command = ["sh", "-c", step]
    stepper.running = true
  }

  Timer {
    id: settle
    interval: 10
    onTriggered: harness.nextPoll()
  }

  Process {
    id: stepper
    onExited: function(exitCode) {
      if (exitCode !== 0) harness.report("step failed: " + stepper.command[2])
      else harness.widget.refresh()
    }
  }

  Loader {
    source: harness.job.widget
    onLoaded: harness.start(item)
    onStatusChanged: if (status === Loader.Error) harness.report("cannot load " + source)
  }
}
