import QtQuick
import QtQuick.Layouts
import Quickshell
import qs.Commons
import qs.Commons as Commons
import qs.Ui
import "Glyphs.js" as G
import "Fmt.js" as Fmt

// Everything inside the library window. View state (page, folder, open
// document) lives here so switching pages keeps your place; data and actions
// come from the service.
Item {
  id: root

  property var service: null
  signal closeRequested()

  property string page: "library"
  property string folderId: ""
  property string viewerId: ""
  property int viewerPage: 0
  property string searchQuery: ""

  readonly property color fg: Commons.Color.foreground
  readonly property color bg: Commons.Color.background
  readonly property color accent: Commons.Color.accent
  readonly property color muted: Util.alpha(fg, 0.62)
  readonly property color faint: Util.alpha(fg, 0.08)
  readonly property string font: Style.font.family

  readonly property var pages: [
    { value: "library", label: "Library", icon: G.library },
    { value: "search", label: "Search", icon: G.search },
    { value: "send", label: "Send", icon: G.send },
    { value: "notes", label: "Notes", icon: G.notes },
    { value: "device", label: "Device", icon: G.tablet },
    { value: "settings", label: "Settings", icon: G.settings }
  ]

  function apply(payloadJson) {
    var p = {}
    try { p = JSON.parse(payloadJson || "{}") } catch (e) {}
    if (p.id) { openDoc(p.id, p.pageIndex || 0); return }
    if (p.query !== undefined) { searchQuery = String(p.query); page = "search"; return }
    if (p.page) page = p.page
    if (p.folder !== undefined) folderId = p.folder
  }

  function openDoc(id, pageIndex) {
    viewerId = id
    viewerPage = pageIndex || 0
    page = "viewer"
  }

  function folderPath(id) {
    if (!service || !id) return ""
    var e = service.entryById(id)
    return e ? String(e.path).replace(/^\//, "") : ""
  }

  // Painted here (not only by the window) so grabToImage screenshots match.
  Rectangle { anchors.fill: parent; color: root.bg }

  focus: true
  Keys.onEscapePressed: {
    if (page === "viewer") page = "library"
    else root.closeRequested()
  }

  RowLayout {
    anchors.fill: parent
    spacing: 0

    // ------------------------------------------------------------ sidebar
    Rectangle {
      Layout.fillHeight: true
      Layout.preferredWidth: Style.space(210)
      color: Util.alpha(root.fg, 0.035)

      ColumnLayout {
        anchors.fill: parent
        anchors.margins: Style.spacing.xxl
        spacing: Style.spacing.sm

        RowLayout {
          spacing: Style.spacing.lg
          Layout.bottomMargin: Style.spacing.xl
          Text {
            text: G.tablet
            color: root.accent
            font.family: root.font
            font.pixelSize: Style.font.display
          }
          ColumnLayout {
            spacing: 0
            Text {
              text: "reMarkable"
              color: root.fg
              font.family: root.font
              font.pixelSize: Style.font.heading
              font.bold: true
            }
            Text {
              text: statusLine()
              color: root.muted
              font.family: root.font
              font.pixelSize: Style.font.caption
              function statusLine() {
                var s = root.service
                if (!s) return "Starting…"
                if (!s.daemonUp) return "Starting the helper…"
                if (!s.paired) return "Not connected"
                if (s.syncError) return "Offline"
                if (s.job) return Fmt.jobLabel(s.job)
                return s.documents.length + " documents"
              }
            }
          }
        }

        Repeater {
          model: root.pages
          delegate: NavItem {
            required property var modelData
            Layout.fillWidth: true
            icon: modelData.icon
            label: modelData.label
            selected: root.page === modelData.value || (modelData.value === "library" && root.page === "viewer")
            onClicked: root.page = modelData.value
          }
        }

        Item { Layout.fillHeight: true }

        // Connection summary, bottom of the sidebar.
        ColumnLayout {
          Layout.fillWidth: true
          spacing: Style.spacing.sm
          StatusChip {
            icon: root.service && root.service.paired && !root.service.syncError ? G.cloudOk : G.cloudOff
            text: {
              var s = root.service
              if (!s || !s.paired) return "Cloud: not connected"
              if (s.syncError) return "Cloud: " + (s.syncError === "not-paired" ? "login expired" : "offline")
              return s.lastSync ? "Synced " + Fmt.ago(s.lastSync) : "Cloud connected"
            }
          }
          StatusChip {
            visible: !!(root.service && root.service.usb && root.service.usb.connected)
            icon: G.usb
            text: root.service && root.service.usb && root.service.usb.webInterface ? "USB: web interface on" : "USB connected"
          }
          StatusChip {
            visible: !!(root.service && root.service.device && root.service.device.battery !== undefined)
            icon: root.service && root.service.device && root.service.device.charging ? G.charging : G.battery
            text: root.service && root.service.device ? "Battery " + Math.round(root.service.device.battery) + "%" : ""
          }
          StatusChip {
            visible: !!(root.service && root.service.printer && root.service.printer.running)
            icon: G.printer
            text: "Printer ready"
          }
        }
      }
    }

    Rectangle { Layout.fillHeight: true; Layout.preferredWidth: 1; color: root.faint }

    // ------------------------------------------------------------- content
    Item {
      Layout.fillWidth: true
      Layout.fillHeight: true

      LibraryPage {
        anchors.fill: parent
        visible: root.page === "library"
        service: root.service
        folderId: root.folderId
        onFolderChosen: function (id) { root.folderId = id }
        onDocumentChosen: function (id) { root.openDoc(id, 0) }
      }

      Loader {
        anchors.fill: parent
        active: root.page === "viewer" && root.viewerId !== ""
        sourceComponent: ViewerPage {
          service: root.service
          docId: root.viewerId
          pageIndex: root.viewerPage
          onBackRequested: root.page = "library"
          onPageChanged: function (i) { root.viewerPage = i }
        }
      }

      Loader {
        anchors.fill: parent
        active: root.page === "search"
        sourceComponent: SearchPage {
          service: root.service
          query: root.searchQuery
          onQueryEdited: function (q) { root.searchQuery = q }
          onResultChosen: function (id, pageIndex) { root.openDoc(id, pageIndex) }
        }
      }

      Loader {
        anchors.fill: parent
        active: root.page === "send"
        sourceComponent: SendPage { service: root.service; currentFolder: root.folderPath(root.folderId) }
      }

      Loader {
        anchors.fill: parent
        active: root.page === "notes"
        sourceComponent: NotesPage { service: root.service }
      }

      Loader {
        anchors.fill: parent
        active: root.page === "device"
        sourceComponent: DevicePage { service: root.service }
      }

      Loader {
        anchors.fill: parent
        active: root.page === "settings"
        sourceComponent: SettingsPage { service: root.service }
      }

      // Notice line: the outcome of the last action.
      Rectangle {
        anchors.right: parent.right
        anchors.bottom: parent.bottom
        anchors.margins: Style.spacing.xxl
        visible: !!(root.service && root.service.notice)
        width: Math.min(noticeText.implicitWidth + Style.spacing.xxl * 2, parent.width - Style.spacing.xxl * 2)
        height: noticeText.implicitHeight + Style.spacing.lg * 2
        radius: Style.cornerRadius
        color: Commons.Color.popups.background
        border.width: 1
        border.color: root.service && root.service.noticeIsError ? Commons.Color.urgent : Util.alpha(root.accent, 0.7)
        Text {
          id: noticeText
          anchors.centerIn: parent
          width: Math.min(implicitWidth, parent.parent.width - Style.spacing.xxl * 4)
          text: root.service ? root.service.notice : ""
          color: root.fg
          elide: Text.ElideRight
          font.family: root.font
          font.pixelSize: Style.font.body
        }
      }
    }
  }

  // Drop files anywhere: they go to the folder you're looking at.
  DropArea {
    id: drop
    anchors.fill: parent
    keys: ["text/uri-list"]
    onDropped: function (ev) {
      var paths = []
      for (var i = 0; i < ev.urls.length; i++) {
        var u = String(ev.urls[i])
        if (u.indexOf("file://") === 0) paths.push(decodeURIComponent(u.slice(7)))
      }
      if (paths.length && root.service) {
        root.service.sendFiles(paths, root.folderPath(root.folderId))
        root.service.say("Sending " + paths.length + (paths.length === 1 ? " file…" : " files…"), false)
      }
      ev.accept()
    }
  }

  Rectangle {
    anchors.fill: parent
    visible: drop.containsDrag
    color: Util.alpha(root.bg, 0.86)
    border.width: 2
    border.color: root.accent
    Column {
      anchors.centerIn: parent
      spacing: Style.spacing.lg
      Text {
        anchors.horizontalCenter: parent.horizontalCenter
        text: G.upload
        color: root.accent
        font.family: root.font
        font.pixelSize: Style.font.displayLarge * 2
      }
      Text {
        anchors.horizontalCenter: parent.horizontalCenter
        text: "Drop to send to " + (root.folderId ? root.folderPath(root.folderId) : "your reMarkable")
        color: root.fg
        font.family: root.font
        font.pixelSize: Style.font.heading
      }
    }
  }
}
