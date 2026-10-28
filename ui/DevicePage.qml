import QtQuick
import QtQuick.Layouts
import Quickshell
import qs.Commons
import qs.Commons as Commons
import qs.Ui
import "Glyphs.js" as G
import "Fmt.js" as Fmt

// The connections: cloud login, the USB cable, and developer-mode SSH.
Flickable {
  id: root
  property var service: null

  readonly property color fg: Commons.Color.foreground
  readonly property color muted: Util.alpha(fg, 0.62)
  readonly property var s: service ? service.settings : ({})
  readonly property var dev: service ? service.device : null

  property string account: ""
  property bool penRunning: false
  property var stream: null
  property string pendingImage: ""

  function busy(key) { var _ = service ? service.busyVersion : 0; return service ? service.isBusy(key) : false }

  Component.onCompleted: {
    if (service && service.paired) service.run(["cloud", "account"], function (r) {
      if (r.ok && r.account) root.account = String(r.account.User || "").split(",")[0]
    })
    refreshDevModeState()
  }

  function refreshDevModeState() {
    service.run(["ssh", "pen", "status"], function (r) { root.penRunning = !!(r.ok && r.running) })
    service.run(["ssh", "stream", "status"], function (r) { root.stream = r.ok && r.running ? r : null })
  }

  function openStream(r) {
    var profile = (Quickshell.env("XDG_CACHE_HOME") || (Quickshell.env("HOME") + "/.cache")) + "/omarchy-remarkable/stream-browser"
    // The stream page has its own sign-in form: put the password on the
    // clipboard and say the username, rather than baking them into the URL.
    if (r.password) root.service.copyText(r.password)
    if (r.username) root.service.say("Sign in as " + r.username + " — the password is on your clipboard")
    var args = ["chromium", "--user-data-dir=" + profile, "--app=" + r.url]
    // Over Wi-Fi the tablet serves HTTPS with its own certificate; this
    // throwaway profile is only ever used for the tablet.
    if (String(r.url).indexOf("https://") === 0) args.splice(2, 0, "--ignore-certificate-errors")
    Quickshell.execDetached(args)
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

    // ------------------------------------------------------------- cloud
    Card {
      title: "reMarkable cloud"
      icon: root.service && root.service.paired ? G.cloudOk : G.cloudOff
      subtitle: root.service && root.service.paired
        ? "Connected" + (root.account ? " as " + root.account : "") + ". This computer shows up on my.remarkable.com as a browser."
        : "Connect once with a one-time code: open the page, copy the 8-letter code, paste it here."

      RowLayout {
        visible: !!root.service && !root.service.paired
        Layout.fillWidth: true
        spacing: Style.spacing.lg
        Button {
          text: "Get a code"
          iconText: G.openExternal
          bordered: true
          onClicked: Quickshell.execDetached(["xdg-open", "https://my.remarkable.com/device/browser/connect"])
        }
        TextField { id: code; Layout.fillWidth: true; placeholderText: "8-letter code"; onAccepted: connectBtn.clicked() }
        Button {
          id: connectBtn
          text: "Connect"
          bordered: true
          enabled: code.text.trim().length === 8
          iconSpinning: root.busy("pair")
          onClicked: root.service.act(["cloud", "pair", code.text.trim()], "pair", "Connected to the reMarkable cloud", function (r) { if (r.ok) { code.text = ""; root.service.refresh() } })
        }
      }
      RowLayout {
        visible: !!root.service && root.service.paired
        spacing: Style.spacing.lg
        Button { text: "Check now"; iconText: G.refresh; bordered: true; onClicked: root.service.refresh() }
        Button { text: "Disconnect this computer"; bordered: true; foreground: Commons.Color.urgent; onClicked: unpairDialog.open() }
      }
    }

    // --------------------------------------------------------------- usb
    Card {
      title: "USB cable"
      icon: G.usb
      subtitle: {
        var u = root.service ? root.service.usb : null
        if (u && u.webInterface) return "Connected, with the USB web interface on."
        if (u && u.connected) return "Connected. Turn on Settings → Storage → USB web interface on the tablet for backups over the cable."
        return "Not connected. Plug the tablet in with USB-C for backups without the cloud, and for developer mode."
      }
      RowLayout {
        spacing: Style.spacing.lg
        Button {
          text: "Back up over USB now"
          iconText: G.backup
          bordered: true
          enabled: !!(root.service && root.service.usb && root.service.usb.webInterface)
          iconSpinning: root.service && root.service.job === "usb-backup"
          onClicked: { root.service.tell("usb-backup"); root.service.say("Backing up over USB…") }
        }
        Button { text: "Open backups"; iconText: G.openExternal; bordered: true; onClicked: root.service.openPath(root.s.backupDir) }
      }
      SettingToggle { service: root.service; key: "usbAutoBackup"; label: "Back up whenever the tablet is plugged in"; description: "Only changed documents are copied; each one is saved as a PDF and in reMarkable's own format." }
    }

    // ---------------------------------------------------------- dev mode
    Card {
      title: "Developer mode"
      icon: G.terminal
      subtitle: root.service && root.service.ssh
        ? "Connected over SSH to " + (root.s.sshHost || "10.11.99.1") + "."
        : "Not reachable at " + (root.s.sshHost || "10.11.99.1") + ". Plug in USB (or enable SSH over Wi-Fi on the tablet and enter its address below), then set up access once."

      // Setup: the root password is on the tablet under
      // Settings → Help → Copyrights and licenses.
      ColumnLayout {
        Layout.fillWidth: true
        visible: !!root.service && !root.service.ssh
        spacing: Style.spacing.md
        Text {
          Layout.fillWidth: true
          text: "First time: the root password is on the tablet in Settings → General → Help → About → Copyrights and licenses. It's used once to install a key, then forgotten."
          color: root.muted
          font.family: Style.font.family
          font.pixelSize: Style.font.caption
          wrapMode: Text.WordWrap
        }
        RowLayout {
          Layout.fillWidth: true
          spacing: Style.spacing.lg
          TextField { id: pw; Layout.fillWidth: true; password: true; placeholderText: "Tablet root password"; onAccepted: setupBtn.clicked() }
          Button {
            id: setupBtn
            text: "Set up access"
            bordered: true
            enabled: pw.text.length > 0
            iconSpinning: root.busy("ssh-setup")
            onClicked: {
              var secret = pw.text
              pw.text = ""
              root.service.runWithInput(["ssh", "setup"], secret, function (r) {
                if (r.ok) { root.service.say("Developer mode access is set up"); root.service.refresh() }
                else root.service.say(r.error || "Setup failed", true)
              }, "ssh-setup")
            }
          }
        }
        SettingText { service: root.service; key: "sshHost"; label: "Tablet address"; description: "10.11.99.1 over USB. Over Wi-Fi, the tablet's IP address." }
      }

      // Info
      GridLayout {
        visible: !!root.dev && !root.dev.error
        columns: 2
        columnSpacing: Style.spacing.huge
        rowSpacing: Style.spacing.sm
        Layout.fillWidth: true
        InfoLabel { text: "Battery" }
        InfoValue { text: root.dev && root.dev.battery !== undefined ? Math.round(root.dev.battery) + "%" + (root.dev.charging ? " · charging" : "") + (root.dev.temperature !== undefined ? " · " + root.dev.temperature + "°C" : "") : "—" }
        InfoLabel { text: "Storage" }
        InfoValue { text: root.dev && root.dev.storageFree !== undefined ? Fmt.bytes(root.dev.storageFree) + " free of " + Fmt.bytes(root.dev.storageTotal) : "—" }
        InfoLabel { text: "Software" }
        InfoValue { text: root.dev ? (root.dev.firmware || "—") : "—" }
        InfoLabel { text: "Model" }
        InfoValue { text: root.dev ? (root.dev.model || "—") : "—" }
        InfoLabel { text: "Wi-Fi address" }
        InfoValue { text: root.dev ? (root.dev.wifiIp || root.dev.wifi_ip || "—") : "—" }
      }

      ColumnLayout {
        Layout.fillWidth: true
        visible: !!root.service && root.service.ssh
        spacing: Style.spacing.lg

        PanelSeparator { Layout.fillWidth: true }

        ActionRow {
          title: "Live screen"
          description: root.stream ? "Streaming. It opens in its own window." : "Watch the tablet's screen in a window, e.g. to share it in a call. Runs a small streaming program on the tablet while it's open."
          Button {
            text: root.stream ? "Show" : "Start"
            iconText: G.cast
            bordered: true
            iconSpinning: root.busy("stream")
            onClicked: {
              if (root.stream) { root.openStream(root.stream); return }
              root.service.run(["ssh", "stream", "start"], function (r) {
                if (!r.ok) { root.service.say(r.error || "Couldn't start the stream", true); return }
                root.stream = r
                root.openStream(r)
              }, "stream")
            }
          }
          Button {
            visible: !!root.stream
            text: "Stop"
            bordered: true
            onClicked: root.service.act(["ssh", "stream", "stop"], "stream", "Stream stopped", function () { root.stream = null })
          }
        }

        ActionRow {
          title: "Pen as a drawing tablet"
          description: root.penRunning ? "On: the Marker moves the pointer on this computer. In Hyprland, input:tablet:output picks the monitor." : "Use the tablet and Marker as a pen tablet for this computer (drawing apps, signatures)."
          Button {
            text: root.penRunning ? "Stop" : "Start"
            iconText: G.pen
            bordered: true
            onClicked: {
              if (root.penRunning) root.service.act(["ssh", "pen", "stop"], "pen", "Pen input stopped", function () { root.penRunning = false })
              else {
                Quickshell.execDetached([root.service.bin, "ssh", "pen", "start"])
                root.service.say("Pen input starting…")
                penCheck.restart()
              }
            }
          }
        }
        Timer { id: penCheck; interval: 2500; onTriggered: root.refreshDevModeState() }

        ActionRow {
          title: "Templates on the desktop"
          description: "Copy the tablet's page templates so notebooks show their lines and grids here too."
          Button {
            text: "Copy now"
            iconText: G.template
            bordered: true
            iconSpinning: root.service && root.service.job === "templates"
            onClicked: { root.service.tell("templates"); root.service.say("Copying templates…") }
          }
        }

        ActionRow {
          title: "Full backup"
          description: "Everything on the tablet in reMarkable's own format, kept in your backups folder (newest five)."
          Button {
            text: "Back up"
            iconText: G.backup
            bordered: true
            iconSpinning: root.busy("ssh-backup")
            onClicked: root.service.act(["ssh", "backup"], "ssh-backup", function (r) { return "Backup saved: " + (r.path || r.file || "") })
          }
        }

        ActionRow {
          title: "Sleep screen"
          description: "Show your own picture when the tablet sleeps. Software updates put the original back."
          Button {
            text: "Choose image…"
            iconText: G.image
            bordered: true
            onClicked: root.service.pick("image", function (path) { if (path) { root.pendingImage = path; sleepDialog.open() } })
          }
          Button {
            text: "Restore"
            bordered: true
            onClicked: root.service.act(["ssh", "sleepscreen", "restore", "--yes"], "sleepscreen", "Original sleep screen restored")
          }
        }

        ActionRow {
          title: "Custom template"
          description: "Add an SVG or PNG page template to the tablet's template list."
          Button {
            text: "Add template…"
            iconText: G.template
            bordered: true
            onClicked: root.service.pick("template", function (path) { if (path) { root.pendingImage = path; templateName.text = path.replace(/^.*\//, "").replace(/\.[^.]*$/, ""); templateDialog.open() } })
          }
        }
      }
    }
  }

  ModalDialog {
    id: unpairDialog
    parent: root.contentItem.parent
    title: "Disconnect this computer from the reMarkable cloud?"
    confirmText: "Disconnect"
    destructive: true
    Text {
      Layout.fillWidth: true
      text: "Nothing is deleted. You can connect again with a new code. To remove it from your account entirely, use my.remarkable.com → Devices and apps."
      color: Commons.Color.popups.text
      font.family: Style.font.family
      font.pixelSize: Style.font.body
      wrapMode: Text.WordWrap
    }
    onConfirmed: root.service.act(["cloud", "unpair"], "unpair", "Disconnected", function () { root.service.refresh() })
  }

  ModalDialog {
    id: sleepDialog
    parent: root.contentItem.parent
    title: "Use this picture as the sleep screen?"
    confirmText: "Put it on the tablet"
    Text {
      Layout.fillWidth: true
      text: root.pendingImage + "\n\nThe original is backed up first, and the tablet's interface restarts for a moment."
      color: Commons.Color.popups.text
      font.family: Style.font.family
      font.pixelSize: Style.font.body
      wrapMode: Text.WrapAnywhere
    }
    onConfirmed: root.service.act(["ssh", "sleepscreen", "set", root.pendingImage, "--yes"], "sleepscreen", "Sleep screen updated")
  }

  ModalDialog {
    id: templateDialog
    parent: root.contentItem.parent
    title: "Add a template to the tablet"
    confirmText: "Add"
    confirmEnabled: templateName.text.trim().length > 0
    TextField { id: templateName; Layout.fillWidth: true; placeholderText: "Template name" }
    Text {
      Layout.fillWidth: true
      text: "The tablet's interface restarts for a moment. Software updates remove custom templates; add them again afterwards."
      color: Commons.Color.popups.text
      font.family: Style.font.family
      font.pixelSize: Style.font.caption
      wrapMode: Text.WordWrap
    }
    onConfirmed: root.service.act(["ssh", "templates", "install", root.pendingImage, "--name", templateName.text.trim(), "--yes"], "template", "Template added")
  }

  component InfoLabel: Text {
    color: Util.alpha(Commons.Color.foreground, 0.62)
    font.family: Style.font.family
    font.pixelSize: Style.font.body
  }
  component InfoValue: Text {
    color: Commons.Color.foreground
    font.family: Style.font.family
    font.pixelSize: Style.font.body
    Layout.fillWidth: true
    elide: Text.ElideRight
  }
}
