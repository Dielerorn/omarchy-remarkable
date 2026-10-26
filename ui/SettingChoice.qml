import QtQuick
import QtQuick.Layouts
import qs.Commons
import qs.Commons as Commons
import qs.Ui

// A small set of mutually exclusive values for one setting.
RowLayout {
  id: root
  property var service: null
  property string key: ""
  property string label: ""
  property var options: []   // [{ value, label }]
  readonly property string value: service && service.settings ? String(service.settings[key]) : ""

  Layout.fillWidth: true
  spacing: Style.spacing.xl
  Text {
    text: root.label
    color: Commons.Color.foreground
    font.family: Style.font.family
    font.pixelSize: Style.font.body
    Layout.fillWidth: true
    wrapMode: Text.WordWrap
  }
  ButtonGroup {
    options: root.options
    value: root.value
    accent: Commons.Color.accent
    onChanged: function (v) { if (root.service) root.service.setSetting(root.key, v) }
  }
}
