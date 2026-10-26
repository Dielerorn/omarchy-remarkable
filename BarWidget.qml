import QtQuick
import QtQuick.Layouts
import qs.Commons
import qs.Commons as Commons
import qs.Ui
import "ui/Glyphs.js" as G
import "ui/Fmt.js" as Fmt

// Bar chip plus popout. One glyph; the sentence goes in the tooltip. The
// popout is for the quick things — send something, open a recent notebook —
// and everything else is a click away in the library window.
Panel {
  id: root

  moduleName: "dielerorn.remarkable"
  ipcTarget: "dielerorn.remarkable"

  readonly property color panelForeground: Commons.Color.popups.text
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family
  readonly property var service: bar && bar.shell ? bar.shell.serviceFor("dielerorn.remarkable") : null
  readonly property bool ready: service !== null
  readonly property int panelWidth: setting("panelWidth", 400)
  readonly property int recentCount: setting("recentCount", 5)

  function busy(key) { var _ = service ? service.busyVersion : 0; return service ? service.isBusy(key) : false }

  function statusText() {
    var s = service
    if (!s) return "Starting…"
    if (!s.daemonUp) return "Starting the helper…"
    if (!s.paired) return "Not connected — open the library to connect"
    if (s.syncError === "not-paired") return "Cloud login expired"
    if (s.syncError) return "Offline"
    if (s.job) return Fmt.jobLabel(s.job)
    return s.documents.length + " documents · synced " + Fmt.ago(s.lastSync)
  }

  function openWindow(payload) {
    if (service) service.openWindow(payload || {})
    root.close()
  }

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  BarIconButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: G.tablet
    active: root.opened || (root.service && root.service.syncing)
    tooltipText: {
      var s = root.service
      if (!s) return "reMarkable"
      var parts = [root.statusText()]
      if (s.device && s.device.battery !== undefined) parts.push("tablet " + Math.round(s.device.battery) + "%" + (s.device.charging ? " charging" : ""))
      if (s.usb && s.usb.connected) parts.push("USB connected")
      return "reMarkable — " + parts.join(" · ")
    }
    onPressed: function (b) { root.toggle() }
  }

  KeyboardPanel {
    id: popup
    anchorItem: button
    bar: root.bar
    owner: root
    open: root.opened
    focusTarget: keyCatcher
    contentWidth: popup.fittedContentWidth(Style.space(root.panelWidth))
    contentHeight: Math.round(Math.min(
      Math.max(popup.verticalContentInset, content.implicitHeight + popup.verticalContentInset),
      popup.availableCardHeight))

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      onCloseRequested: root.close()
      onTabRequested: function (direction) { root.switchPanel(direction) }

      Column {
        id: content
        width: parent.width
        spacing: Style.spacing.md

        PanelHero {
          width: parent.width
          title: "reMarkable"
          meta: root.statusText()
          foreground: root.panelForeground
          fontFamily: root.fontFamily
          iconComponent: Component {
            Text {
              text: G.tablet
              color: Commons.Color.accent
              font.family: root.fontFamily
              font.pixelSize: Style.font.display
            }
          }
          trailingControl: Component {
            PanelActionButton {
              iconText: G.refresh
              tooltipText: "Check the cloud now"
              foreground: root.panelForeground
              onClicked: if (root.service) root.service.refresh()
            }
          }
        }

        // Device line, only when there's something to say.
        Text {
          width: parent.width
          visible: text !== ""
          text: {
            var s = root.service
            if (!s) return ""
            var parts = []
            if (s.device && s.device.battery !== undefined) parts.push((s.device.charging ? G.charging : G.battery) + " " + Math.round(s.device.battery) + "%")
            if (s.usb && s.usb.connected) parts.push(G.usb + " USB")
            if (s.printer && s.printer.running) parts.push(G.printer + " printer ready")
            return parts.join("    ")
          }
          color: Util.alpha(root.panelForeground, 0.7)
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
        }

        PanelSeparator { width: parent.width }

        // Quick actions, two per row.
        Grid {
          width: parent.width
          columns: 2
          columnSpacing: Style.spacing.md
          rowSpacing: Style.spacing.md
          QuickButton { text: "Library"; iconText: G.library; onClicked: root.openWindow({ page: "library" }) }
          QuickButton { text: "Search"; iconText: G.search; onClicked: root.openWindow({ page: "search" }) }
          QuickButton { text: "Send files…"; iconText: G.upload; iconSpinning: root.busy("send"); onClicked: { if (root.service) root.service.pickAndSend(); root.close() } }
          QuickButton { text: "Send screenshot"; iconText: G.screenshot; iconSpinning: root.busy("screenshot"); onClicked: if (root.service) root.service.sendScreenshot() }
          QuickButton { text: "Send copied link"; iconText: G.web; iconSpinning: root.busy("web"); onClicked: if (root.service) root.service.sendClipboardUrl() }
          QuickButton { text: "Today's agenda"; iconText: G.calendar; iconSpinning: root.busy("agenda"); onClicked: if (root.service) root.service.sendAgenda() }
        }

        PanelSeparator { width: parent.width; visible: recentRepeater.count > 0 }
        PanelSectionHeader { text: "Recent"; foreground: root.panelForeground; visible: recentRepeater.count > 0 }

        Repeater {
          id: recentRepeater
          model: root.service && root.service.paired ? root.service.recent.slice(0, root.recentCount) : []
          delegate: Rectangle {
            required property var modelData
            width: content.width
            height: Style.space(48)
            radius: Style.cornerRadius
            color: rowMouse.containsMouse ? Util.alpha(root.panelForeground, 0.07) : "transparent"
            Component.onCompleted: if (root.service) root.service.requestThumb(modelData.id)

            RowLayout {
              anchors.fill: parent
              anchors.leftMargin: Style.spacing.md
              anchors.rightMargin: Style.spacing.md
              spacing: Style.spacing.lg
              Rectangle {
                Layout.preferredWidth: Style.space(30)
                Layout.preferredHeight: Style.space(40)
                color: "#f4f2ec"
                clip: true
                Image {
                  anchors.fill: parent
                  source: root.service ? root.service.thumb(modelData.id) : ""
                  fillMode: Image.PreserveAspectCrop
                  verticalAlignment: Image.AlignTop
                  asynchronous: true
                  cache: false
                }
              }
              ColumnLayout {
                Layout.fillWidth: true
                spacing: 0
                Text {
                  Layout.fillWidth: true
                  text: modelData.name
                  color: root.panelForeground
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.body
                  elide: Text.ElideRight
                }
                Text {
                  Layout.fillWidth: true
                  text: Fmt.modified(modelData.modified) + " · " + String(modelData.path).replace(/\/[^\/]*$/, "").replace(/^\//, "")
                  color: Util.alpha(root.panelForeground, 0.6)
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.caption
                  elide: Text.ElideRight
                }
              }
            }
            MouseArea {
              id: rowMouse
              anchors.fill: parent
              hoverEnabled: true
              cursorShape: Qt.PointingHandCursor
              onClicked: root.openWindow({ id: modelData.id })
            }
          }
        }

        Text {
          width: parent.width
          visible: !!(root.service && root.service.notice)
          text: root.service ? root.service.notice : ""
          color: root.service && root.service.noticeIsError ? Commons.Color.urgent : Util.alpha(root.panelForeground, 0.75)
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
          wrapMode: Text.WordWrap
        }
      }
    }
  }

  component QuickButton: Button {
    width: (content.width - Style.spacing.md) / 2
    bordered: true
    leftAlign: true
    foreground: root.panelForeground
    fontFamily: root.fontFamily
  }
}
