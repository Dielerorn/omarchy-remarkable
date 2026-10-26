import QtQuick
import QtQuick.Layouts
import qs.Commons
import qs.Commons as Commons
import qs.Ui
import "Glyphs.js" as G
import "Fmt.js" as Fmt

// Read one document, a page at a time, as the tablet draws it.
Item {
  id: root

  property var service: null
  property string docId: ""
  property int pageIndex: 0
  signal backRequested()
  signal pageChanged(int index)

  property var info: null
  property string imageUrl: ""
  property string loadError: ""
  property bool fitWidth: false
  property bool textPanel: false
  property var pageText: ({})       // index -> { typed, highlights[], handwriting }
  property bool textLoading: false

  readonly property int pageCount: info ? info.pageCount : 0
  readonly property color fg: Commons.Color.foreground
  readonly property color muted: Util.alpha(fg, 0.62)

  focus: true
  Keys.onLeftPressed: go(pageIndex - 1)
  Keys.onRightPressed: go(pageIndex + 1)
  Keys.onPressed: function (ev) {
    if (ev.key === Qt.Key_PageDown || ev.key === Qt.Key_Space) { go(pageIndex + 1); ev.accepted = true }
    else if (ev.key === Qt.Key_PageUp) { go(pageIndex - 1); ev.accepted = true }
    else if (ev.key === Qt.Key_Home) { go(0); ev.accepted = true }
    else if (ev.key === Qt.Key_End) { go(pageCount - 1); ev.accepted = true }
  }
  Keys.onEscapePressed: root.backRequested()

  onDocIdChanged: load()
  Component.onCompleted: { load(); forceActiveFocus() }

  function load() {
    if (!service || !docId) return
    info = null
    imageUrl = ""
    loadError = ""
    pageText = ({})
    service.run(["doc", "info", docId], function (r) {
      if (!r.ok) { root.loadError = r.error || "Couldn't open this document"; return }
      root.info = r
      if (root.pageIndex >= r.pageCount) root.pageIndex = Math.max(0, r.pageCount - 1)
      root.showPage()
      root.loadText()
    }, "info:" + docId)
  }

  function go(i) {
    if (!info || i < 0 || i >= pageCount || i === pageIndex) return
    pageIndex = i
    root.pageChanged(i)
    showPage()
  }

  function showPage() {
    var wanted = pageIndex
    loadError = ""
    service.run(["doc", "page", docId, String(wanted), "--width", "1400"], function (r) {
      if (wanted !== root.pageIndex) return
      if (!r.ok) { root.loadError = r.error || "Couldn't draw this page"; return }
      root.imageUrl = "file://" + r.path
      // Draw the next page in the background so paging forward is instant.
      if (wanted + 1 < root.pageCount) root.service.run(["doc", "page", root.docId, String(wanted + 1), "--width", "1400"], null, "prefetch:" + root.docId + ":" + (wanted + 1))
    }, "page:" + docId + ":" + wanted)
  }

  function loadText() {
    textLoading = true
    service.run(["highlights", docId, "--format", "json"], function (r) {
      root.textLoading = false
      if (!r.ok) return
      var byPage = {}
      var pages = r.pages || []
      for (var i = 0; i < pages.length; i++) {
        var p = pages[i]
        var idx = p.index !== undefined ? p.index : (p.page - 1)
        byPage[idx] = { typed: p.typed || p.text || "", highlights: p.highlights || [], handwriting: p.handwriting || p.transcription || "" }
      }
      root.pageText = byPage
    }, "text:" + docId)
  }

  function transcribePage() {
    var n = pageIndex + 1
    service.act(["transcribe", docId, "--pages", String(n)], "transcribe:" + docId + ":" + n,
                "Read the handwriting on page " + n, function () { root.loadText() })
    service.say("Reading the handwriting on page " + n + "…")
  }

  readonly property var current: pageText[pageIndex] || null

  ColumnLayout {
    anchors.fill: parent
    spacing: 0

    // ------------------------------------------------------------ toolbar
    Rectangle {
      Layout.fillWidth: true
      Layout.preferredHeight: Style.spacing.controlHeight + Style.spacing.xxl * 2
      color: Util.alpha(root.fg, 0.03)

      RowLayout {
        anchors.fill: parent
        anchors.leftMargin: Style.spacing.xl
        anchors.rightMargin: Style.spacing.xl
        spacing: Style.spacing.lg

        PanelActionButton { iconText: G.back; tooltipText: "Back to the library (Esc)"; onClicked: root.backRequested() }
        ColumnLayout {
          Layout.fillWidth: true
          spacing: 0
          Text {
            Layout.fillWidth: true
            text: root.info ? root.info.name : "Opening…"
            color: root.fg
            font.family: Style.font.family
            font.pixelSize: Style.font.heading
            font.bold: true
            elide: Text.ElideRight
          }
          Text {
            Layout.fillWidth: true
            text: root.info ? [String(root.info.path).replace(/\/[^\/]*$/, "") || "/", Fmt.typeLabel(root.info.fileType), "edited " + Fmt.modified(root.info.modified)].join(" · ") : ""
            color: root.muted
            font.family: Style.font.family
            font.pixelSize: Style.font.caption
            elide: Text.ElideRight
          }
        }

        PanelActionButton { iconText: G.prev; tooltipText: "Previous page"; enabled: root.pageIndex > 0; onClicked: root.go(root.pageIndex - 1) }
        Text {
          text: root.pageCount ? (root.pageIndex + 1) + " / " + root.pageCount : ""
          color: root.fg
          font.family: Style.font.family
          font.pixelSize: Style.font.body
        }
        PanelActionButton { iconText: G.next; tooltipText: "Next page"; enabled: root.pageIndex + 1 < root.pageCount; onClicked: root.go(root.pageIndex + 1) }

        ButtonGroup {
          options: [{ value: "page", label: "Page" }, { value: "width", label: "Width" }]
          value: root.fitWidth ? "width" : "page"
          accent: Commons.Color.accent
          onChanged: function (v) { root.fitWidth = (v === "width") }
        }
        PanelActionButton { iconText: G.notes; tooltipText: "Text, highlights and handwriting"; onClicked: root.textPanel = !root.textPanel }
        PanelActionButton {
          iconText: G.openExternal
          tooltipText: "Open in your PDF viewer"
          onClicked: { root.service.act(["doc", "open", root.docId], "open:" + root.docId, "Opened in your PDF viewer"); root.service.say("Preparing…") }
        }
        PanelActionButton {
          iconText: G.exportIcon
          tooltipText: "Export as PDF"
          onClicked: root.service.act(["doc", "export", root.docId, "--format", "pdf", "--open"], "export:" + root.docId, function (r) { return "Saved " + r.paths[0] })
        }
      }
    }

    RowLayout {
      Layout.fillWidth: true
      Layout.fillHeight: true
      spacing: 0

      // -------------------------------------------------------------- page
      Item {
        Layout.fillWidth: true
        Layout.fillHeight: true
        clip: true

        Flickable {
          id: flick
          anchors.fill: parent
          anchors.margins: Style.spacing.xl
          contentWidth: width
          contentHeight: root.fitWidth ? Math.max(height, page.paintedHeight) : height
          boundsBehavior: Flickable.StopAtBounds
          interactive: root.fitWidth

          Rectangle {
            anchors.horizontalCenter: parent.horizontalCenter
            y: 0
            width: page.paintedWidth + 2
            height: page.paintedHeight + 2
            color: "transparent"
            border.width: 1
            border.color: Util.alpha(root.fg, 0.15)
            visible: page.status === Image.Ready
          }
          Image {
            id: page
            x: 1
            y: 1
            width: flick.width - 2
            height: root.fitWidth ? implicitHeight * (width / Math.max(1, implicitWidth)) : flick.height - 2
            source: root.imageUrl
            fillMode: Image.PreserveAspectFit
            verticalAlignment: Image.AlignTop
            asynchronous: true
            cache: false
            smooth: true
            mipmap: true
          }
        }

        Text {
          anchors.centerIn: parent
          visible: root.loadError !== "" || (root.imageUrl === "" && !root.loadError)
          text: root.loadError || "Drawing the page…"
          color: root.loadError ? Commons.Color.urgent : root.muted
          font.family: Style.font.family
          font.pixelSize: Style.font.body
          wrapMode: Text.WordWrap
          width: Math.min(parent.width - 40, implicitWidth)
        }

        // Click the left/right third of the page to turn it, like the tablet.
        MouseArea {
          anchors.fill: parent
          enabled: !root.fitWidth
          onClicked: function (ev) {
            if (ev.x < width / 3) root.go(root.pageIndex - 1)
            else if (ev.x > width * 2 / 3) root.go(root.pageIndex + 1)
          }
          onWheel: function (ev) { root.go(root.pageIndex + (ev.angleDelta.y < 0 ? 1 : -1)) }
        }
      }

      // --------------------------------------------------------- text panel
      Rectangle {
        Layout.fillHeight: true
        Layout.preferredWidth: root.textPanel ? Style.space(340) : 0
        visible: root.textPanel
        color: Util.alpha(root.fg, 0.03)

        Flickable {
          anchors.fill: parent
          anchors.margins: Style.spacing.xl
          contentHeight: textCol.implicitHeight
          clip: true
          boundsBehavior: Flickable.StopAtBounds

          ColumnLayout {
            id: textCol
            width: parent.width
            spacing: Style.spacing.xl

            Text {
              text: "Page " + (root.pageIndex + 1)
              color: root.fg
              font.family: Style.font.family
              font.pixelSize: Style.font.title
              font.bold: true
            }

            PanelSectionHeader { text: "Typed text"; visible: !!(root.current && root.current.typed) }
            Text {
              Layout.fillWidth: true
              visible: !!(root.current && root.current.typed)
              text: root.current ? root.current.typed : ""
              color: root.fg
              textFormat: Text.MarkdownText
              wrapMode: Text.WordWrap
              font.family: Style.font.family
              font.pixelSize: Style.font.body
            }

            PanelSectionHeader { text: "Highlights"; visible: !!(root.current && root.current.highlights.length) }
            Repeater {
              model: root.current ? root.current.highlights : []
              delegate: Rectangle {
                required property var modelData
                Layout.fillWidth: true
                implicitHeight: hl.implicitHeight + Style.spacing.lg * 2
                color: Util.alpha(root.fg, 0.04)
                radius: Style.cornerRadius
                Rectangle { width: 3; height: parent.height; color: modelData.hex || modelData.color_hex || "#e0c000" }
                Text {
                  id: hl
                  anchors.fill: parent
                  anchors.margins: Style.spacing.lg
                  anchors.leftMargin: Style.spacing.xl
                  text: typeof modelData === "string" ? modelData : (modelData.text || "")
                  color: root.fg
                  wrapMode: Text.WordWrap
                  font.family: Style.font.family
                  font.pixelSize: Style.font.body
                }
              }
            }

            PanelSectionHeader { text: "Handwriting" }
            Text {
              Layout.fillWidth: true
              visible: !!(root.current && root.current.handwriting)
              text: root.current ? root.current.handwriting : ""
              color: root.fg
              textFormat: Text.MarkdownText
              wrapMode: Text.WordWrap
              font.family: Style.font.family
              font.pixelSize: Style.font.body
            }
            Text {
              Layout.fillWidth: true
              visible: !(root.current && root.current.handwriting)
              text: "Not read yet. Reading sends this page's image to Claude."
              color: root.muted
              wrapMode: Text.WordWrap
              font.family: Style.font.family
              font.pixelSize: Style.font.caption
            }
            Button {
              text: root.current && root.current.handwriting ? "Read again" : "Read this page's handwriting"
              iconText: G.handwriting
              bordered: true
              iconSpinning: { var _ = root.service.busyVersion; return root.service.isBusy("transcribe:" + root.docId + ":" + (root.pageIndex + 1)) }
              onClicked: root.transcribePage()
            }
          }
        }
      }
    }
  }
}
