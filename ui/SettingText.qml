import QtQuick
import QtQuick.Layouts
import qs.Commons
import qs.Commons as Commons
import qs.Ui

// Label + text field bound to one setting; saves when you press Enter or leave.
ColumnLayout {
  id: root
  property var service: null
  property string key: ""
  property string label: ""
  property string description: ""
  property string placeholder: ""
  readonly property string value: service && service.settings && service.settings[key] !== undefined ? String(service.settings[key]) : ""

  Layout.fillWidth: true
  spacing: Style.spacing.xs

  Text {
    text: root.label
    color: Commons.Color.foreground
    font.family: Style.font.family
    font.pixelSize: Style.font.body
  }
  TextField {
    id: field
    Layout.fillWidth: true
    text: root.value
    placeholderText: root.placeholder
    onEditingFinished: if (root.service && text !== root.value) root.service.setSetting(root.key, text)
  }
  Text {
    visible: root.description !== ""
    text: root.description
    color: Util.alpha(Commons.Color.foreground, 0.6)
    font.family: Style.font.family
    font.pixelSize: Style.font.caption
    wrapMode: Text.WordWrap
    Layout.fillWidth: true
  }
}
