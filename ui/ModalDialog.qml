import QtQuick
import QtQuick.Layouts
import qs.Commons
import qs.Commons as Commons
import qs.Ui

// In-window modal: scrim, a card with a title, any content, and two buttons.
Item {
  id: root
  property bool opened: false
  property string title: ""
  property string confirmText: "OK"
  property string cancelText: "Cancel"
  property bool destructive: false
  property bool confirmEnabled: true
  default property alias content: body.data
  signal confirmed()
  signal canceled()

  anchors.fill: parent
  visible: opened
  z: 100

  function open() { opened = true; Qt.callLater(function () { root.forceActiveFocus() }) }
  function close() { opened = false }

  Keys.onEscapePressed: { root.close(); root.canceled() }

  Rectangle {
    anchors.fill: parent
    color: Util.alpha(Commons.Color.background, 0.72)
    MouseArea { anchors.fill: parent; onClicked: { root.close(); root.canceled() } }
  }

  Rectangle {
    anchors.centerIn: parent
    width: Math.min(parent.width - Style.space(60), Style.space(460))
    height: col.implicitHeight + Style.spacing.huge * 2
    radius: Style.cornerRadius
    color: Commons.Color.popups.background
    border.width: 1
    border.color: Commons.Color.popups.border
    MouseArea { anchors.fill: parent }

    ColumnLayout {
      id: col
      anchors.fill: parent
      anchors.margins: Style.spacing.huge
      spacing: Style.spacing.xl
      Text {
        text: root.title
        color: Commons.Color.popups.text
        font.family: Style.font.family
        font.pixelSize: Style.font.title
        font.bold: true
        wrapMode: Text.WordWrap
        Layout.fillWidth: true
      }
      ColumnLayout { id: body; Layout.fillWidth: true; spacing: Style.spacing.lg }
      RowLayout {
        Layout.alignment: Qt.AlignRight
        spacing: Style.spacing.lg
        Button {
          text: root.cancelText
          bordered: true
          onClicked: { root.close(); root.canceled() }
        }
        Button {
          text: root.confirmText
          bordered: true
          enabled: root.confirmEnabled
          foreground: root.destructive ? Commons.Color.urgent : Commons.Color.accent
          onClicked: { root.close(); root.confirmed() }
        }
      }
    }
  }
}
