import QtQuick
import QtQuick.Layouts
import qs.Commons
import qs.Commons as Commons

// A titled section on a settings-style page.
Rectangle {
  id: root
  property string title: ""
  property string icon: ""
  property string subtitle: ""
  default property alias content: body.data

  Layout.fillWidth: true
  implicitHeight: col.implicitHeight + Style.spacing.xxl * 2
  radius: Style.cornerRadius
  color: Util.alpha(Commons.Color.foreground, 0.035)
  border.width: 1
  border.color: Util.alpha(Commons.Color.foreground, 0.08)

  ColumnLayout {
    id: col
    anchors.left: parent.left
    anchors.right: parent.right
    anchors.top: parent.top
    anchors.margins: Style.spacing.xxl
    spacing: Style.spacing.lg

    RowLayout {
      spacing: Style.spacing.lg
      visible: root.title !== ""
      Text {
        visible: root.icon !== ""
        text: root.icon
        color: Commons.Color.accent
        font.family: Style.font.family
        font.pixelSize: Style.font.iconLarge
      }
      ColumnLayout {
        spacing: 0
        Layout.fillWidth: true
        Text {
          text: root.title
          color: Commons.Color.foreground
          font.family: Style.font.family
          font.pixelSize: Style.font.title
          font.bold: true
        }
        Text {
          visible: root.subtitle !== ""
          text: root.subtitle
          color: Util.alpha(Commons.Color.foreground, 0.62)
          font.family: Style.font.family
          font.pixelSize: Style.font.caption
          wrapMode: Text.WordWrap
          Layout.fillWidth: true
        }
      }
    }

    ColumnLayout {
      id: body
      Layout.fillWidth: true
      spacing: Style.spacing.lg
    }
  }
}
