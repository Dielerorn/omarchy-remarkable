import QtQuick
import QtQuick.Layouts
import qs.Commons
import qs.Commons as Commons
import qs.Ui
import "Glyphs.js" as G
import "Fmt.js" as Fmt

// Turning the tablet's contents into Markdown, text and a local copy.
Flickable {
  id: root
  property var service: null

  readonly property color fg: Commons.Color.foreground
  readonly property color muted: Util.alpha(fg, 0.62)
  readonly property var s: service ? service.settings : ({})
  property var stats: null

  function busy(key) { var _ = service ? service.busyVersion : 0; return service ? service.isBusy(key) : false }

  Component.onCompleted: service.run(["index", "stats"], function (r) { if (r.ok) root.stats = r })

  function toggleAutoFolder(path) {
    var list = (root.s.autoTranscribeFolders || []).slice()
    var i = list.indexOf(path)
    if (i >= 0) list.splice(i, 1)
    else list.push(path)
    root.service.setSetting("autoTranscribeFolders", list)
  }

  contentHeight: col.implicitHeight + Style.spacing.huge * 2
  clip: true
  boundsBehavior: Flickable.StopAtBounds

  ColumnLayout {
    id: col
    x: Style.spacing.huge
    y: Style.spacing.huge
    width: Math.min(root.width - Style.spacing.huge * 2, Style.space(760))
    spacing: Style.spacing.xl

    Card {
      title: "Markdown notes"
      icon: G.markdown
      subtitle: "One note per document with its typed text, highlights, handwriting and page images — ready for Obsidian. Anything you write below “your notes below” in a note is kept when it's updated."
      RowLayout {
        Layout.fillWidth: true
        spacing: Style.spacing.lg
        SettingText { service: root.service; key: "notesDir"; label: "Notes folder (point it inside an Obsidian vault if you like)"; Layout.fillWidth: true }
        Button { text: "Open"; iconText: G.openExternal; bordered: true; Layout.alignment: Qt.AlignBottom; onClicked: root.service.openPath(root.s.notesDir) }
      }
      SettingToggle { service: root.service; key: "notesIncludeImages"; label: "Include page images"; description: "Pages with ink are saved as images next to the note and embedded in it." }
      RowLayout {
        spacing: Style.spacing.lg
        Button {
          text: "Write notes for everything"
          iconText: G.markdown
          bordered: true
          iconSpinning: root.busy("notes-all")
          onClicked: { root.service.act(["notes", "sync", "--all"], "notes-all", function (r) { return "Wrote " + (r.written !== undefined ? r.written : (r.paths ? r.paths.length : "")) + " notes" }); root.service.say("Writing notes for every document…") }
        }
        Text { text: "Right-click a document or folder in the library to do just that one."; color: root.muted; font.family: Style.font.family; font.pixelSize: Style.font.caption; wrapMode: Text.WordWrap; Layout.fillWidth: true }
      }
    }

    Card {
      title: "Handwriting to text"
      icon: G.handwriting
      subtitle: "Pages are read by Claude, keeping the original language and script. Each page is sent once; unchanged pages are never sent again."
      SettingChoice {
        service: root.service
        key: "transcribeEngine"
        label: "Read handwriting with"
        options: [{ value: "claude-cli", label: "Claude Code" }, { value: "anthropic-api", label: "API key" }, { value: "off", label: "Off" }]
      }
      Text {
        Layout.fillWidth: true
        text: root.s.transcribeEngine === "anthropic-api" ? "Uses ANTHROPIC_API_KEY from the environment; billed per page." : "Uses your Claude Code login (the claude command)."
        color: root.muted
        font.family: Style.font.family
        font.pixelSize: Style.font.caption
        wrapMode: Text.WordWrap
      }
      SettingText { service: root.service; key: "transcribeModel"; label: "Model" }
      SettingText { service: root.service; key: "transcribeLanguages"; label: "Languages you write in (optional)"; placeholder: "English and Russian"; Layout.fillWidth: true }
      PanelSectionHeader { text: "Read new handwriting automatically in these folders" }
      Flow {
        Layout.fillWidth: true
        spacing: Style.spacing.md
        Repeater {
          model: root.service ? root.service.folders.filter(function (f) { return f.parent === "" }).sort(function (a, b) { return String(a.name).localeCompare(String(b.name)) }) : []
          delegate: Rectangle {
            required property var modelData
            readonly property bool on: (root.s.autoTranscribeFolders || []).indexOf(String(modelData.path)) >= 0
            radius: height / 2
            implicitHeight: chip.implicitHeight + Style.spacing.sm * 2
            implicitWidth: chip.implicitWidth + Style.spacing.xl * 2
            color: on ? Util.alpha(Commons.Color.accent, 0.2) : Util.alpha(root.fg, 0.05)
            border.width: 1
            border.color: on ? Commons.Color.accent : Util.alpha(root.fg, 0.12)
            Text { id: chip; anchors.centerIn: parent; text: (parent.on ? G.check + " " : "") + modelData.name; color: parent.on ? Commons.Color.accent : root.fg; font.family: Style.font.family; font.pixelSize: Style.font.body }
            MouseArea { anchors.fill: parent; cursorShape: Qt.PointingHandCursor; onClicked: root.toggleAutoFolder(String(modelData.path)) }
          }
        }
      }
      Text {
        Layout.fillWidth: true
        text: "At most 30 pages are read per check, so a big folder catches up over a few rounds."
        color: root.muted
        font.family: Style.font.family
        font.pixelSize: Style.font.caption
        wrapMode: Text.WordWrap
      }
    }

    Card {
      title: "Local copy"
      icon: G.pdf
      subtitle: "Every document as a PDF with your ink, in folders like the tablet's. Updated when something changes; files are only ever replaced, never edited by you here."
      SettingToggle { service: root.service; key: "mirrorEnabled"; label: "Keep a local copy" }
      RowLayout {
        Layout.fillWidth: true
        spacing: Style.spacing.lg
        SettingText { service: root.service; key: "mirrorDir"; label: "Folder"; Layout.fillWidth: true }
        Button { text: "Open"; iconText: G.openExternal; bordered: true; Layout.alignment: Qt.AlignBottom; onClicked: root.service.openPath(root.s.mirrorDir) }
        Button {
          text: "Update now"
          iconText: G.refresh
          bordered: true
          Layout.alignment: Qt.AlignBottom
          iconSpinning: root.service && root.service.job === "mirror"
          onClicked: { root.service.tell("mirror"); root.service.say("Updating the local copy…") }
        }
      }
    }

    Card {
      title: "Search index"
      icon: G.search
      subtitle: root.stats ? (root.stats.documents || 0) + " documents indexed" + (root.stats.size ? " · " + Fmt.bytes(root.stats.size) : "") + ". It updates itself when documents change." : "Builds itself in the background."
      Button {
        text: "Update now"
        iconText: G.refresh
        bordered: true
        iconSpinning: root.service && root.service.job === "index"
        onClicked: { root.service.tell("index"); root.service.say("Updating the search index…") }
      }
    }
  }
}
