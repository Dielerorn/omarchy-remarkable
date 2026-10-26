import QtQuick
import QtQuick.Layouts
import qs.Commons
import qs.Commons as Commons
import "Glyphs.js" as G

// A flat, indented list of every cloud folder (plus the top level).
Rectangle {
  id: root
  property var service: null
  property string selected: ""
  property string exclude: ""
  signal picked(string id)

  color: Util.alpha(Commons.Color.foreground, 0.04)
  radius: Style.cornerRadius
  border.width: 1
  border.color: Util.alpha(Commons.Color.foreground, 0.1)

  function rows() {
    if (!service) return []
    var _ = service.libraryVersion
    var out = [{ id: "", name: "My files", depth: 0 }]
    var walk = function (parentId, depth) {
      var kids = service.childrenOf(parentId).filter(function (e) { return e.folder && e.id !== root.exclude })
      kids.sort(function (a, b) { return String(a.name).localeCompare(String(b.name)) })
      for (var i = 0; i < kids.length; i++) {
        out.push({ id: kids[i].id, name: kids[i].name, depth: depth })
        walk(kids[i].id, depth + 1)
      }
    }
    walk("", 1)
    return out
  }

  ListView {
    anchors.fill: parent
    anchors.margins: Style.spacing.sm
    clip: true
    model: root.rows()
    delegate: Rectangle {
      required property var modelData
      width: ListView.view.width
      height: Style.spacing.popupRowHeight
      radius: Style.cornerRadius
      color: root.selected === modelData.id ? Util.alpha(Commons.Color.accent, 0.18) : (m.containsMouse ? Util.alpha(Commons.Color.foreground, 0.06) : "transparent")
      Row {
        anchors.verticalCenter: parent.verticalCenter
        x: Style.spacing.lg + modelData.depth * Style.spacing.xxl
        spacing: Style.spacing.md
        Text { text: modelData.depth === 0 ? G.home : G.folder; color: Commons.Color.accent; font.family: Style.font.family; font.pixelSize: Style.font.icon }
        Text { text: modelData.name; color: Commons.Color.popups.text; font.family: Style.font.family; font.pixelSize: Style.font.body }
      }
      MouseArea { id: m; anchors.fill: parent; hoverEnabled: true; onClicked: root.picked(modelData.id) }
    }
  }
}
