import QtQuick
import QtQuick.Layouts
import qs.Commons
import qs.Commons as Commons

Rectangle {
  id: root
  property string icon: ""
  property string label: ""
  property bool selected: false
  signal clicked()

  implicitHeight: Style.spacing.controlHeight + Style.spacing.md
  radius: Style.cornerRadius
  color: selected ? Util.alpha(Commons.Color.accent, 0.16) : (mouse.containsMouse ? Util.alpha(Commons.Color.foreground, 0.06) : "transparent")

  RowLayout {
    anchors.fill: parent
    anchors.leftMargin: Style.spacing.lg
    anchors.rightMargin: Style.spacing.lg
    spacing: Style.spacing.lg
    Text {
      text: root.icon
      color: root.selected ? Commons.Color.accent : Commons.Color.foreground
      font.family: Style.font.family
      font.pixelSize: Style.font.iconLarge
      Layout.preferredWidth: Style.font.iconLarge + Style.spacing.sm
    }
    Text {
      text: root.label
      color: root.selected ? Commons.Color.accent : Commons.Color.foreground
      font.family: Style.font.family
      font.pixelSize: Style.font.subtitle
      Layout.fillWidth: true
    }
  }

  MouseArea {
    id: mouse
    anchors.fill: parent
    hoverEnabled: true
    cursorShape: Qt.PointingHandCursor
    onClicked: root.clicked()
  }
}
