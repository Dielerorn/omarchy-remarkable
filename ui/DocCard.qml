import QtQuick
import QtQuick.Layouts
import qs.Commons
import qs.Commons as Commons
import "Glyphs.js" as G
import "Fmt.js" as Fmt

// One tile in the library grid: a folder, or a document with its cover.
Item {
  id: root
  property var service: null
  property var entry: ({})
  property int itemCount: 0
  signal activated()
  signal menuRequested(real x, real y)

  readonly property bool isFolder: !!entry.folder
  readonly property string thumbUrl: service && !isFolder ? service.thumb(entry.id) : ""

  Component.onCompleted: if (service && !isFolder) service.requestThumb(entry.id)

  Rectangle {
    id: card
    anchors.fill: parent
    anchors.margins: Style.spacing.md
    radius: Style.cornerRadius
    color: mouse.containsMouse ? Util.alpha(Commons.Color.foreground, 0.07) : "transparent"

    ColumnLayout {
      anchors.fill: parent
      anchors.margins: Style.spacing.md
      spacing: Style.spacing.sm

      // Cover / folder art, paper-coloured like the tablet.
      Rectangle {
        Layout.fillWidth: true
        Layout.fillHeight: true
        radius: Math.max(2, Style.cornerRadius / 2)
        color: root.isFolder ? Util.alpha(Commons.Color.accent, 0.10) : "#f4f2ec"
        border.width: 1
        border.color: Util.alpha(Commons.Color.foreground, 0.12)
        clip: true

        Image {
          anchors.fill: parent
          anchors.margins: 1
          visible: !root.isFolder && root.thumbUrl !== ""
          source: root.thumbUrl
          fillMode: Image.PreserveAspectCrop
          verticalAlignment: Image.AlignTop
          asynchronous: true
          cache: false
          smooth: true
          mipmap: true
        }
        Text {
          anchors.centerIn: parent
          visible: root.isFolder || root.thumbUrl === ""
          text: root.isFolder ? G.folder : G.forType(root.entry.fileType)
          color: root.isFolder ? Commons.Color.accent : "#9a978f"
          font.family: Style.font.family
          font.pixelSize: Math.round(parent.width * 0.32)
        }
        Text {
          visible: !!root.entry.starred
          anchors.top: parent.top
          anchors.right: parent.right
          anchors.margins: Style.spacing.sm
          text: G.star
          color: "#e0a800"
          font.family: Style.font.family
          font.pixelSize: Style.font.iconLarge
        }
      }

      Text {
        Layout.fillWidth: true
        text: root.entry.name || ""
        color: Commons.Color.foreground
        font.family: Style.font.family
        font.pixelSize: Style.font.body
        elide: Text.ElideRight
        maximumLineCount: 2
        wrapMode: Text.Wrap
        Layout.preferredHeight: Style.font.body * 2.6
        verticalAlignment: Text.AlignTop
      }
      Text {
        Layout.fillWidth: true
        text: root.isFolder
          ? (root.itemCount === 1 ? "1 item" : root.itemCount + " items")
          : [Fmt.modified(root.entry.modified), Fmt.typeLabel(root.entry.fileType),
             root.entry.pageCount ? root.entry.pageCount + " p" : ""].filter(function (s) { return s }).join(" · ")
        color: Util.alpha(Commons.Color.foreground, 0.55)
        font.family: Style.font.family
        font.pixelSize: Style.font.caption
        elide: Text.ElideRight
      }
    }
  }

  MouseArea {
    id: mouse
    anchors.fill: parent
    hoverEnabled: true
    acceptedButtons: Qt.LeftButton | Qt.RightButton
    cursorShape: Qt.PointingHandCursor
    onClicked: function (ev) {
      if (ev.button === Qt.RightButton) root.menuRequested(ev.x, ev.y)
      else root.activated()
    }
  }
}
