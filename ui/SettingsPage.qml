import QtQuick
import QtQuick.Layouts
import qs.Commons
import qs.Commons as Commons
import qs.Ui
import "Glyphs.js" as G

Flickable {
  id: root
  property var service: null
  readonly property var s: service ? service.settings : ({})
  readonly property color muted: Util.alpha(Commons.Color.foreground, 0.62)

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
      title: "General"
      icon: G.settings
      SettingToggle { service: root.service; key: "notifyNewNotes"; label: "Notify me when something changes on the tablet"; description: "Changes this computer made itself are not announced." }
      SettingText { service: root.service; key: "pollSeconds"; label: "Check the cloud every (seconds)"; description: "At least 30." }
    }

    Card {
      title: "Folders on this computer"
      icon: G.folder
      SettingText { service: root.service; key: "exportDir"; label: "Exports" }
      SettingText { service: root.service; key: "notesDir"; label: "Markdown notes" }
      SettingText { service: root.service; key: "mirrorDir"; label: "Local copy" }
      SettingText { service: root.service; key: "sendDir"; label: "Send folder" }
      SettingText { service: root.service; key: "backupDir"; label: "Backups" }
    }

    Card {
      title: "Folders on the tablet"
      icon: G.tablet
      subtitle: "Folder paths like Books/Articles. Empty means the top level. Missing folders are created."
      SettingText { service: root.service; key: "uploadFolder"; label: "Files you send" }
      SettingText { service: root.service; key: "webFolder"; label: "Web pages" }
      SettingText { service: root.service; key: "printFolder"; label: "Printed documents" }
      SettingText { service: root.service; key: "screenshotFolder"; label: "Screenshots" }
      SettingText { service: root.service; key: "agendaFolder"; label: "Agenda pages" }
    }

    Card {
      id: desktopCard
      title: "Desktop"
      icon: G.send
      subtitle: "Optional extras outside the plugin folder. Nothing here is added until you ask."
      property var info: ({})
      function refresh() {
        if (root.service) root.service.run(["desktop", "status"], function (r) { if (r.ok) desktopCard.info = r })
      }
      function change(action, part, done) {
        root.service.act(["desktop", action, part], "desktop", done, function (r) { if (r.ok) desktopCard.info = r })
      }
      Component.onCompleted: refresh()

      ActionRow {
        title: "Send to reMarkable in Open With menus"
        description: "Adds a menu entry to ~/.local/share/applications, so file managers can send files to the tablet."
        Button {
          text: desktopCard.info.openWith ? "Remove" : "Add"
          bordered: true
          onClicked: desktopCard.info.openWith
            ? desktopCard.change("remove", "--open-with", "Removed from Open With menus")
            : desktopCard.change("add", "--open-with", "Added to Open With menus")
        }
      }
      ActionRow {
        title: "omarchy-remarkable command"
        description: desktopCard.info.commandTaken
          ? "~/.local/bin/omarchy-remarkable already exists and belongs to something else, so it's left alone."
          : "Links the plugin's command into ~/.local/bin, for scripts and keybindings."
        Button {
          visible: !desktopCard.info.commandTaken
          text: desktopCard.info.command ? "Remove" : "Add"
          bordered: true
          onClicked: desktopCard.info.command
            ? desktopCard.change("remove", "--command", "Command link removed")
            : desktopCard.change("add", "--command", "omarchy-remarkable is on your PATH")
        }
      }
      Text {
        Layout.fillWidth: true
        text: "To make this window float and stay opaque, add the window rule from the plugin's README to ~/.config/hypr/windows.lua."
        color: root.muted
        font.family: Style.font.family
        font.pixelSize: Style.font.caption
        wrapMode: Text.WordWrap
      }
    }

    Card {
      title: "About"
      icon: G.tablet
      Text {
        Layout.fillWidth: true
        text: "reMarkable for Omarchy " + (root.service && root.service.manifest ? root.service.manifest.version : "") + "\nHelper: omarchy-remarkable (bin/omarchy-remarkable in the plugin folder). Settings live in ~/.config/omarchy-remarkable/settings.json."
        color: root.muted
        font.family: Style.font.family
        font.pixelSize: Style.font.caption
        wrapMode: Text.WordWrap
      }
    }
  }
}
