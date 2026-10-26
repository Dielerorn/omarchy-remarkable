import QtQuick
import QtQuick.Layouts
import qs.Commons
import qs.Commons as Commons

RowLayout {
  id: root
  property string icon: ""
  property string text: ""
  spacing: Style.spacing.md
  Layout.fillWidth: true
  Text {
    text: root.icon
    color: Util.alpha(Commons.Color.foreground, 0.75)
    font.family: Style.font.family
    font.pixelSize: Style.font.body
  }
  Text {
    text: root.text
    color: Util.alpha(Commons.Color.foreground, 0.75)
    font.family: Style.font.family
    font.pixelSize: Style.font.caption
    elide: Text.ElideRight
    Layout.fillWidth: true
  }
}
