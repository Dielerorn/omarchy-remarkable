import QtQuick
import QtQuick.Layouts
import qs.Commons
import qs.Commons as Commons
import qs.Ui
import "Glyphs.js" as G

// Every way of getting something onto the tablet, on one page.
Flickable {
  id: root
  property var service: null
  property string currentFolder: ""

  readonly property color fg: Commons.Color.foreground
  readonly property color muted: Util.alpha(fg, 0.62)
  readonly property var s: service ? service.settings : ({})

  function busy(key) { var _ = service ? service.busyVersion : 0; return service ? service.isBusy(key) : false }

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
      title: "Files"
      icon: G.upload
      subtitle: "PDF and EPUB go as they are; images, text and Markdown are turned into tablet-sized PDFs. You can also drop files anywhere in this window."
      RowLayout {
        spacing: Style.spacing.lg
        Button {
          text: "Choose files…"
          iconText: G.upload
          bordered: true
          iconSpinning: root.busy("send")
          onClicked: root.service.pickAndSend(root.currentFolder)
        }
        Text {
          text: "to " + (root.currentFolder || root.s.uploadFolder || "My files")
          color: root.muted
          font.family: Style.font.family
          font.pixelSize: Style.font.body
        }
      }
    }

    Card {
      title: "Web page"
      icon: G.web
      subtitle: "Sends a clean, readable copy of an article. EPUB reflows on the tablet; PDF keeps a fixed layout."
      RowLayout {
        Layout.fillWidth: true
        spacing: Style.spacing.lg
        TextField {
          id: url
          Layout.fillWidth: true
          placeholderText: "https://…"
          onAccepted: if (text.trim()) root.service.sendUrl(text.trim(), format.value)
        }
        ButtonGroup {
          id: format
          options: [{ value: "epub", label: "EPUB" }, { value: "pdf", label: "PDF" }]
          value: root.s.webPageFormat || "epub"
          accent: Commons.Color.accent
          onChanged: function (v) { format.value = v }
        }
        Button {
          text: "Send"
          iconText: G.send
          bordered: true
          enabled: url.text.trim().length > 0
          iconSpinning: root.busy("web")
          onClicked: root.service.sendUrl(url.text.trim(), format.value)
        }
      }
      RowLayout {
        spacing: Style.spacing.lg
        Button {
          text: "Send the link on the clipboard"
          iconText: G.clipboard
          bordered: true
          onClicked: root.service.sendClipboardUrl()
        }
        Text {
          text: "Bindable: omarchy-shell remarkable sendClipboardUrl"
          color: root.muted
          font.family: Style.font.family
          font.pixelSize: Style.font.caption
        }
      }
      SettingText { service: root.service; key: "webFolder"; label: "Tablet folder for web pages" }
    }

    Card {
      title: "Screenshot"
      icon: G.screenshot
      subtitle: "Sends your newest screenshot (Omarchy's or Framelet's)."
      RowLayout {
        spacing: Style.spacing.lg
        Button {
          text: "Send the latest screenshot"
          iconText: G.screenshot
          bordered: true
          iconSpinning: root.busy("screenshot")
          onClicked: root.service.sendScreenshot()
        }
        Text {
          text: "Bindable: omarchy-shell remarkable sendScreenshot"
          color: root.muted
          font.family: Style.font.family
          font.pixelSize: Style.font.caption
        }
      }
      SettingText { service: root.service; key: "screenshotFolder"; label: "Tablet folder for screenshots" }
    }

    Card {
      title: "Morning agenda"
      icon: G.calendar
      subtitle: "A page with today's calendar and tasks, and room to write. Uses your Google Calendar from the Omarchy calendar."
      RowLayout {
        spacing: Style.spacing.lg
        Button {
          text: "Send today's agenda now"
          iconText: G.calendar
          bordered: true
          iconSpinning: root.busy("agenda")
          onClicked: root.service.sendAgenda()
        }
      }
      SettingToggle { service: root.service; key: "agendaEnabled"; label: "Send one every morning"; description: "Sent once a day, at the time below or as soon as the computer is on after it." }
      SettingText { service: root.service; key: "agendaTime"; label: "Time (24-hour, HH:MM)" }
      SettingText { service: root.service; key: "agendaFolder"; label: "Tablet folder for agenda pages" }
    }

    Card {
      title: "Print to reMarkable"
      icon: G.printer
      subtitle: root.service && root.service.printer && root.service.printer.running
        ? "Ready. Choose “reMarkable” in any app's print dialog and the pages arrive on the tablet."
        : "A printer called “reMarkable” that sends whatever you print to the tablet. It runs as your user — nothing system-wide."
      SettingToggle { service: root.service; key: "printerEnabled"; label: "Offer the reMarkable printer" }
      SettingText { service: root.service; key: "printFolder"; label: "Tablet folder for printed documents" }
      Text {
        visible: !!(root.service && root.service.printer && root.service.printer.error)
        text: root.service && root.service.printer ? (root.service.printer.error || "") : ""
        color: Commons.Color.urgent
        font.family: Style.font.family
        font.pixelSize: Style.font.caption
        wrapMode: Text.WordWrap
        Layout.fillWidth: true
      }
    }

    Card {
      title: "Send folder"
      icon: G.folder
      subtitle: "Anything saved into this folder is sent to the tablet, then moved to its Sent folder. Subfolders become tablet folders."
      SettingToggle { service: root.service; key: "sendFolderEnabled"; label: "Watch the send folder" }
      RowLayout {
        Layout.fillWidth: true
        spacing: Style.spacing.lg
        SettingText { service: root.service; key: "sendDir"; label: "Folder"; Layout.fillWidth: true }
        Button {
          text: "Open"
          iconText: G.openExternal
          bordered: true
          Layout.alignment: Qt.AlignBottom
          onClicked: root.service.openPath(root.s.sendDir)
        }
      }
    }

    Card {
      title: "From anywhere"
      icon: G.terminal
      subtitle: "“Send to reMarkable” appears in your file manager's Open With menu. From a terminal:"
      Text {
        Layout.fillWidth: true
        text: "omarchy-remarkable send report.pdf notes.md photo.jpg\nomarchy-remarkable web https://example.com/article\nomarchy-remarkable agenda send"
        color: root.fg
        font.family: "monospace"
        font.pixelSize: Style.font.bodySmall
        wrapMode: Text.WrapAnywhere
      }
    }
  }
}
