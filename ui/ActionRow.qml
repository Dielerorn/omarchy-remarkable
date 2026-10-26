import QtQuick
import QtQuick.Layouts
import qs.Commons
import qs.Commons as Commons

// Title and explanation on the left, buttons on the right.
RowLayout {
  id: root
  property string title: ""
  property string description: ""
  default property alias buttons: actions.data

  Layout.fillWidth: true
  spacing: Style.spacing.xl

  ColumnLayout {
    Layout.fillWidth: true
    spacing: 1
    Text {
      text: root.title
      color: Commons.Color.foreground
      font.family: Style.font.family
      font.pixelSize: Style.font.body
      font.bold: true
    }
    Text {
      text: root.description
      color: Util.alpha(Commons.Color.foreground, 0.62)
      font.family: Style.font.family
      font.pixelSize: Style.font.caption
      wrapMode: Text.WordWrap
      Layout.fillWidth: true
    }
  }
  RowLayout { id: actions; spacing: Style.spacing.md }
}
