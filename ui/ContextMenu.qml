import QtQuick
import QtQuick.Layouts
import qs.Commons
import qs.Commons as Commons
import "Glyphs.js" as G

// Right-click menu for a library tile. Actions that need a dialog are
// signalled back to the page; the rest run straight through the service.
Item {
  id: root
  property var service: null
  property var target: null
  property bool opened: false
  signal openRequested(var entry)
  signal renameRequested(var entry)
  signal moveRequested(var entry)
  signal trashRequested(var entry)

  anchors.fill: parent
  visible: opened
  z: 90

  function openFor(entry, x, y) {
    target = entry
    opened = true
    panel.x = Math.min(x, root.width - panel.width - Style.spacing.lg)
    panel.y = Math.min(y, root.height - panel.height - Style.spacing.lg)
  }

  readonly property var actions: !target ? [] : (target.folder ? [
    { id: "open", label: "Open", icon: G.folder },
    { id: "rename", label: "Rename…", icon: G.rename },
    { id: "move", label: "Move…", icon: G.folderMove },
    { id: "notes", label: "Write notes for this folder", icon: G.markdown },
    { id: "trash", label: "Move to trash…", icon: G.trash, danger: true }
  ] : [
    { id: "open", label: "Open", icon: G.eye },
    { id: "app", label: "Open in PDF viewer", icon: G.openExternal },
    { id: "pdf", label: "Export as PDF", icon: G.exportIcon },
    { id: "png", label: "Export pages as images", icon: G.image },
    { id: "original", label: "Save the original file", icon: G.download },
    { id: "notes", label: "Write a Markdown note", icon: G.markdown },
    { id: "transcribe", label: "Turn handwriting into text", icon: G.handwriting },
    { id: "rename", label: "Rename…", icon: G.rename },
    { id: "move", label: "Move…", icon: G.folderMove },
    { id: "trash", label: "Move to trash…", icon: G.trash, danger: true }
  ])

  function run(action) {
    var e = target
    opened = false
    if (!e || !service) return
    var s = service
    switch (action) {
    case "open": openRequested(e); break
    case "rename": renameRequested(e); break
    case "move": moveRequested(e); break
    case "trash": trashRequested(e); break
    case "app": s.act(["doc", "open", e.id], "open:" + e.id, "Opened " + e.name); s.say("Preparing " + e.name + "…"); break
    case "pdf": s.act(["doc", "export", e.id, "--format", "pdf", "--open"], "export:" + e.id, function (r) { return "Saved " + r.paths[0] }); break
    case "png": s.act(["doc", "export", e.id, "--format", "png", "--open"], "export:" + e.id, function (r) { return "Saved " + r.paths.length + " images" }); break
    case "original": s.act(["doc", "export", e.id, "--format", "original", "--open"], "export:" + e.id, function (r) { return "Saved " + r.paths[0] }); break
    case "notes":
      if (e.folder) s.act(["notes", "sync", "--folder", String(e.path)], "notes", function (r) { return "Notes written to " + (r.dir || "your notes folder") })
      else s.act(["notes", "sync", e.id], "notes:" + e.id, function (r) { return "Note written: " + (r.path || (r.paths && r.paths[0]) || "") })
      s.say("Writing notes…")
      break
    case "transcribe": s.act(["transcribe", e.id], "transcribe:" + e.id, function (r) { return "Handwriting read on " + (r.transcribed !== undefined ? r.transcribed : (r.pages ? r.pages.length : "")) + " pages" }); s.say("Reading handwriting in " + e.name + "…"); break
    }
  }

  MouseArea {
    anchors.fill: parent
    acceptedButtons: Qt.LeftButton | Qt.RightButton
    onClicked: root.opened = false
  }

  Rectangle {
    id: panel
    width: Style.space(250)
    height: col.implicitHeight + Style.spacing.md * 2
    radius: Style.cornerRadius
    color: Commons.Color.menu.background
    border.width: 1
    border.color: Util.alpha(Commons.Color.foreground, 0.2)

    Column {
      id: col
      anchors.fill: parent
      anchors.margins: Style.spacing.md
      Repeater {
        model: root.actions
        delegate: Rectangle {
          required property var modelData
          width: col.width
          height: Style.spacing.popupRowHeight
          radius: Style.cornerRadius
          color: hover.containsMouse ? Commons.Color.menu.selectedBackground : "transparent"
          RowLayout {
            anchors.fill: parent
            anchors.leftMargin: Style.spacing.lg
            anchors.rightMargin: Style.spacing.lg
            spacing: Style.spacing.lg
            Text {
              text: modelData.icon
              color: modelData.danger ? Commons.Color.urgent : Commons.Color.menu.text
              font.family: Style.font.family
              font.pixelSize: Style.font.icon
            }
            Text {
              Layout.fillWidth: true
              text: modelData.label
              color: modelData.danger ? Commons.Color.urgent : (hover.containsMouse ? Commons.Color.menu.selectedText : Commons.Color.menu.text)
              font.family: Style.font.family
              font.pixelSize: Style.font.body
              elide: Text.ElideRight
            }
          }
          MouseArea {
            id: hover
            anchors.fill: parent
            hoverEnabled: true
            cursorShape: Qt.PointingHandCursor
            onClicked: root.run(modelData.id)
          }
        }
      }
    }
  }
}
