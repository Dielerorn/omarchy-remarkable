import QtQuick
import Quickshell
import Quickshell.Hyprland
import qs.Commons
import qs.Commons as Commons
import qs.Ui
import "ui"

// The library window: a real toplevel (FloatingWindow), one instance.
// Summoning it again with a payload ({page, id, query}) navigates the open
// window instead of opening a second one.
Item {
  id: host

  property var shell: null
  property var manifest: null
  property var service: null

  readonly property string pluginId: "dielerorn.remarkable"
  readonly property var svc: service || (shell && typeof shell.serviceFor === "function" ? shell.serviceFor(pluginId) : null)

  property bool shown: false
  property string pendingPayload: "{}"
  property bool closingFromHost: false

  function open(payloadJson) {
    pendingPayload = payloadJson && String(payloadJson).length ? String(payloadJson) : "{}"
    if (!shown) {
      shown = true
    } else {
      if (main.item) main.item.apply(pendingPayload)
      raiseTimer.restart()
    }
  }

  function close() {
    closingFromHost = true
    shown = false
    closingFromHost = false
  }

  function toggle(payloadJson) {
    if (shown) requestClose()
    else open(payloadJson)
  }

  function requestClose() {
    if (shell && typeof shell.hide === "function") shell.hide(pluginId)
    else close()
  }

  // Save the window's content as an image (screenshots for docs and tests).
  function grab(path) {
    if (!main.item) return
    main.item.grabToImage(function (result) { result.saveToFile(path) })
  }

  Timer {
    id: raiseTimer
    interval: 90
    onTriggered: Hyprland.dispatch("hl.dsp.focus({ window = \"title:^reMarkable$\" })")
  }

  FloatingWindow {
    id: window
    visible: host.shown
    title: "reMarkable"
    color: Commons.Color.background
    implicitWidth: 1180
    implicitHeight: 780
    minimumSize: Qt.size(760, 480)

    onVisibleChanged: {
      if (visible || host.closingFromHost) return
      // Closed by the compositor (SUPER+W): tell the shell so it re-summons cleanly.
      host.shown = false
      host.requestClose()
    }

    Loader {
      id: main
      anchors.fill: parent
      active: host.shown
      sourceComponent: Main {
        service: host.svc
        onCloseRequested: host.requestClose()
      }
      onLoaded: item.apply(host.pendingPayload)
    }
  }
}
