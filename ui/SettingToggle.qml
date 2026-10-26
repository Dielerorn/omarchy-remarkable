import QtQuick
import QtQuick.Layouts
import qs.Commons
import qs.Commons as Commons
import qs.Ui

// A setting row whose switch writes straight to the helper's settings.json.
RowLayout {
  id: root
  property var service: null
  property string key: ""
  property string label: ""
  property string description: ""
  readonly property bool checked: !!(service && service.settings && service.settings[key])

  Layout.fillWidth: true
  spacing: Style.spacing.xl

  ColumnLayout {
    Layout.fillWidth: true
    spacing: 1
    Text {
      text: root.label
      color: Commons.Color.foreground
      font.family: Style.font.family
      font.pixelSize: Style.font.body
      wrapMode: Text.WordWrap
      Layout.fillWidth: true
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

  ToggleSwitch {
    checked: root.checked
    accent: Commons.Color.accent
    onToggled: if (root.service) root.service.setSetting(root.key, !root.checked)
  }
}
