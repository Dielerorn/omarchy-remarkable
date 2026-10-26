import QtQuick
import QtQuick.Layouts
import qs.Commons
import qs.Commons as Commons
import qs.Ui
import "Glyphs.js" as G
import "Fmt.js" as Fmt

// Search names, typed text, highlights, PDF text and read handwriting.
Item {
  id: root
  property var service: null
  property string query: ""
  signal queryEdited(string q)
  signal resultChosen(string id, int pageIndex)

  property var results: []
  property bool searching: false
  property string error: ""
  property var stats: null

  readonly property color fg: Commons.Color.foreground
  readonly property color muted: Util.alpha(fg, 0.62)

  readonly property var sourceLabels: ({ name: "Title", typed: "Typed", highlight: "Highlight", handwriting: "Handwriting", pdf: "Text" })

  Component.onCompleted: {
    field.text = query
    field.forceActiveFocus()
    if (query) runSearch()
    service.run(["index", "stats"], function (r) { if (r.ok) root.stats = r })
  }

  Timer { id: debounce; interval: 280; onTriggered: root.runSearch() }

  function runSearch() {
    var q = field.text.trim()
    if (!q) { results = []; error = ""; return }
    searching = true
    service.run(["search", q, "--limit", "60"], function (r) {
      root.searching = false
      if (q !== field.text.trim()) return
      if (!r.ok) { root.error = r.error || "Search failed"; root.results = []; return }
      root.error = ""
      root.results = r.results || []
    })
  }

  ColumnLayout {
    anchors.fill: parent
    anchors.margins: Style.spacing.huge
    spacing: Style.spacing.xl

    RowLayout {
      Layout.fillWidth: true
      spacing: Style.spacing.lg
      Text { text: G.search; color: Commons.Color.accent; font.family: Style.font.family; font.pixelSize: Style.font.display }
      TextField {
        id: field
        Layout.fillWidth: true
        placeholderText: "Search titles, typed text, highlights, PDFs and handwriting"
        onTextChanged: { root.queryEdited(text); debounce.restart() }
        onAccepted: root.runSearch()
      }
    }

    Text {
      Layout.fillWidth: true
      text: {
        if (root.error) return root.error
        if (root.searching) return "Searching…"
        if (field.text.trim() && root.results.length === 0) return "No matches."
        if (!field.text.trim() && root.stats) return "Searching " + (root.stats.documents || 0) + " documents" + (root.stats.pages ? " · " + root.stats.pages + " pages of text" : "") + (root.stats.handwritingPages ? " · " + root.stats.handwritingPages + " pages of handwriting" : "") + "."
        return root.results.length ? root.results.length + " matches" : ""
      }
      color: root.error ? Commons.Color.urgent : root.muted
      font.family: Style.font.family
      font.pixelSize: Style.font.caption
      wrapMode: Text.WordWrap
    }

    RowLayout {
      visible: !!root.stats && !root.stats.documents
      spacing: Style.spacing.lg
      Text {
        text: "The search index is empty."
        color: root.fg
        font.family: Style.font.family
        font.pixelSize: Style.font.body
      }
      Button {
        text: "Build it now"
        bordered: true
        onClicked: { root.service.tell("index"); root.service.say("Building the search index in the background…") }
      }
    }

    ListView {
      Layout.fillWidth: true
      Layout.fillHeight: true
      clip: true
      spacing: Style.spacing.sm
      model: root.results
      boundsBehavior: Flickable.StopAtBounds
      delegate: Rectangle {
        required property var modelData
        width: ListView.view.width
        implicitHeight: col.implicitHeight + Style.spacing.lg * 2
        radius: Style.cornerRadius
        color: m.containsMouse ? Util.alpha(root.fg, 0.07) : Util.alpha(root.fg, 0.03)

        ColumnLayout {
          id: col
          anchors.left: parent.left
          anchors.right: parent.right
          anchors.verticalCenter: parent.verticalCenter
          anchors.margins: Style.spacing.xl
          spacing: Style.spacing.xs
          RowLayout {
            spacing: Style.spacing.lg
            Layout.fillWidth: true
            Text {
              text: modelData.name || ""
              color: root.fg
              font.family: Style.font.family
              font.pixelSize: Style.font.subtitle
              font.bold: true
              elide: Text.ElideRight
              Layout.fillWidth: true
            }
            Rectangle {
              radius: height / 2
              color: Util.alpha(Commons.Color.accent, 0.16)
              implicitWidth: badge.implicitWidth + Style.spacing.lg * 2
              implicitHeight: badge.implicitHeight + Style.spacing.xs * 2
              Text { id: badge; anchors.centerIn: parent; text: root.sourceLabels[modelData.source] || modelData.source; color: Commons.Color.accent; font.family: Style.font.family; font.pixelSize: Style.font.caption }
            }
            Text {
              visible: !!modelData.page
              text: "p. " + modelData.page
              color: root.muted
              font.family: Style.font.family
              font.pixelSize: Style.font.caption
            }
          }
          Text {
            Layout.fillWidth: true
            text: Fmt.snippet(modelData.snippet || "")
            textFormat: Text.StyledText
            color: Util.alpha(root.fg, 0.85)
            font.family: Style.font.family
            font.pixelSize: Style.font.body
            wrapMode: Text.WordWrap
            maximumLineCount: 3
            elide: Text.ElideRight
          }
          Text {
            Layout.fillWidth: true
            text: modelData.path || ""
            color: root.muted
            font.family: Style.font.family
            font.pixelSize: Style.font.caption
            elide: Text.ElideMiddle
          }
        }
        MouseArea {
          id: m
          anchors.fill: parent
          hoverEnabled: true
          cursorShape: Qt.PointingHandCursor
          onClicked: root.resultChosen(modelData.id, Math.max(0, (modelData.page || 1) - 1))
        }
      }
    }
  }
}
