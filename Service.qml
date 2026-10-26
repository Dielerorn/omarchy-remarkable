import QtQuick
import Quickshell
import Quickshell.Io

// Owner of everything that outlives a popout or a window: the helper daemon,
// the library snapshot, thumbnails, and the command runner. The bar widget
// and the library window both reach it through shell.serviceFor().
//
// The rule: no reMarkable logic lives in QML. Every action is a call to
// bin/omarchy-remarkable, which prints one JSON object; the daemon streams
// state as JSON lines. QML only shows that state and asks for things.
QtObject {
  id: root

  property var shell: null
  property var manifest: null

  readonly property string pluginId: "dielerorn.remarkable"
  readonly property string bin: String(Qt.resolvedUrl("bin/omarchy-remarkable")).replace(/^file:\/\//, "")
  readonly property string home: Quickshell.env("HOME") || ""

  // ------------------------------------------------------------ daemon state

  property bool daemonUp: false
  property int daemonRestarts: 0
  property bool paired: false
  property var entries: []
  property real lastSync: 0
  property string syncError: ""
  property var usb: ({ connected: false, webInterface: false })
  property bool ssh: false
  property var device: null
  property var printer: ({ running: false })
  property string job: ""
  property var queued: []
  property var settings: ({})
  property var recentChanges: []
  property string lastJobError: ""

  // Bumped whenever entries change so views can re-evaluate cheap bindings.
  property int libraryVersion: 0

  readonly property var documents: entries.filter(function (e) { return !e.folder && !e.trashed })
  readonly property var folders: entries.filter(function (e) { return e.folder && !e.trashed && e.id !== "trash" })
  readonly property var recent: documents.slice().sort(function (a, b) {
    return String(b.modified).localeCompare(String(a.modified))
  })
  readonly property bool syncing: job !== "" || queued.length > 0

  // Shown briefly in the window/popout footer.
  property string notice: ""
  property bool noticeIsError: false

  function say(text, isError) {
    notice = text
    noticeIsError = !!isError
    noticeTimer.restart()
  }

  function entryById(id) {
    for (var i = 0; i < entries.length; i++) if (entries[i].id === id) return entries[i]
    return null
  }

  function childrenOf(parentId) {
    return entries.filter(function (e) { return e.parent === parentId && !e.trashed && e.id !== "trash" })
  }

  function handleEvent(line) {
    var ev
    try { ev = JSON.parse(line) } catch (e) { return }
    switch (ev.event) {
    case "hello":
      daemonUp = true
      daemonRestarts = 0
      paired = !!ev.paired
      break
    case "status":
      paired = !!ev.paired
      lastSync = ev.lastSync || lastSync
      syncError = ev.syncError || ""
      usb = ev.usb || usb
      ssh = !!ev.ssh
      if (!ev.ssh) device = null
      else if (ev.device) device = ev.device
      printer = ev.printer || printer
      job = ev.job || ""
      queued = ev.queued || []
      if (ev.settings) settings = ev.settings
      break
    case "library":
      entries = ev.entries || []
      libraryVersion++
      if (ev.changes && ev.changes.length) {
        var keep = ev.changes.filter(function (c) { return c.kind !== "removed" && !c.self })
        recentChanges = keep.concat(recentChanges).slice(0, 20)
        for (var i = 0; i < ev.changes.length; i++) delete thumbs[ev.changes[i].id]
        thumbsVersion++
      }
      break
    case "usb":
      usb = ev
      break
    case "device":
      device = ev
      break
    case "job":
      if (ev.state === "start") job = ev.name
      else if (ev.state === "error") {
        lastJobError = ev.name + ": " + ev.error
        if (ev.code !== "not-paired") say(ev.error, true)
      }
      break
    case "duplicate":
      // Another daemon (an older shell's child) still holds the lock; ours
      // waits on it and takes over, nothing to do.
      break
    }
  }

  property Process daemon: Process {
    command: [root.bin, "daemon"]
    running: true
    stdinEnabled: true
    stdout: SplitParser {
      splitMarker: "\n"
      onRead: function (line) { root.handleEvent(line) }
    }
    onExited: function (code) {
      root.daemonUp = false
      root.daemonRestarts++
      restartTimer.restart()
    }
  }

  property Timer restartTimer: Timer {
    interval: Math.min(60000, 2000 * Math.max(1, root.daemonRestarts))
    repeat: false
    onTriggered: root.daemon.running = true
  }

  property Timer noticeTimer: Timer {
    interval: 6000
    onTriggered: root.notice = ""
  }

  function tell(command) {
    if (daemon.running) daemon.write(command + "\n")
  }

  function refresh() { tell("refresh") }

  // ---------------------------------------------------------- command runner

  // Commands in flight, keyed by a caller-chosen name, so buttons can spin.
  property var busy: ({})
  property int busyVersion: 0

  function isBusy(key) { return !!busy[key] }

  property Component runner: Component {
    Process {
      id: proc
      property var callback: null
      property string key: ""
      stdout: StdioCollector {
        waitForEnd: true
        onStreamFinished: {
          var result
          var raw = String(text || "").trim()
          var last = raw.lastIndexOf("\n{")
          try { result = JSON.parse(last >= 0 ? raw.slice(last + 1) : raw) }
          catch (e) { result = { ok: false, error: raw ? raw.slice(-300) : "no answer from the helper" } }
          proc.finish(result)
        }
      }
      property bool done: false
      function finish(result) {
        if (done) return
        done = true
        if (key) { delete root.busy[key]; root.busyVersion++ }
        if (callback) {
          try { callback(result) } catch (e) { console.warn("remarkable: callback failed", e) }
        }
        destroy(1000)
      }
    }
  }

  // run(["doc", "thumb", id], function (r) {...}, "thumb:" + id)
  function run(args, callback, key) {
    if (key) {
      if (busy[key]) return false
      busy[key] = true
      busyVersion++
    }
    var p = runner.createObject(root, { command: [root.bin].concat(args) })
    p.callback = callback || null
    p.key = key || ""
    p.running = true
    return true
  }

  // Like run(), but writes `input` to the command's stdin and closes it —
  // used for the tablet's root password so it never appears in argv.
  function runWithInput(args, input, callback, key) {
    if (key) {
      if (busy[key]) return false
      busy[key] = true
      busyVersion++
    }
    var p = runner.createObject(root, { command: [root.bin].concat(args), stdinEnabled: true })
    p.callback = callback || null
    p.key = key || ""
    p.running = true
    p.write(String(input) + "\n")
    p.stdinEnabled = false
    return true
  }

  // Ask for a file with the desktop chooser; callback gets a path or "".
  function pick(kind, callback) {
    run(["pick", "--kind", kind || "any"], function (r) {
      callback(r.ok && r.paths && r.paths.length ? r.paths[0] : "")
    })
  }

  // Run and report the outcome in the notice line.
  function act(args, key, success, after) {
    return run(args, function (r) {
      if (r.ok) { if (success) say(typeof success === "function" ? success(r) : success, false) }
      else say(r.error || "Something went wrong", true)
      if (after) after(r)
    }, key)
  }

  // -------------------------------------------------------------- thumbnails

  property var thumbs: ({})
  property int thumbsVersion: 0
  property var thumbQueue: []
  property int thumbsRunning: 0

  function thumb(id) {
    var _ = thumbsVersion
    return thumbs[id] || ""
  }

  function requestThumb(id) {
    if (!id || thumbs[id] || thumbQueue.indexOf(id) >= 0 || busy["thumb:" + id]) return
    thumbQueue.push(id)
    pumpThumbs()
  }

  function pumpThumbs() {
    while (thumbsRunning < 2 && thumbQueue.length > 0) {
      var id = thumbQueue.shift()
      thumbsRunning++
      run(["doc", "thumb", id, "--width", "360"], (function (docId) {
        return function (r) {
          root.thumbsRunning--
          if (r.ok) { root.thumbs[docId] = "file://" + r.path; root.thumbsVersion++ }
          root.pumpThumbs()
        }
      })(id), "thumb:" + id)
    }
  }

  // ---------------------------------------------------------------- windows

  function openWindow(payload) {
    if (shell) shell.summon(pluginId, payload ? JSON.stringify(payload) : "{}")
  }

  function toggleWindow() {
    if (shell && typeof shell.toggle === "function") shell.toggle(pluginId, "{}")
    else openWindow({})
  }

  // ------------------------------------------------------- common actions

  function sendFiles(paths, folder) {
    var args = ["send"].concat(paths)
    if (folder !== undefined && folder !== null) args = args.concat(["--folder", folder])
    return act(args, "send", function (r) {
      var n = (r.sent || []).length
      return n === 1 ? "Sent " + r.sent[0].name : "Sent " + n + " files"
    }, function () { root.refresh() })
  }

  function pickAndSend(folder) {
    var args = ["send-pick"]
    if (folder) args = args.concat(["--folder", folder])
    return act(args, "send", function (r) {
      var n = (r.sent || []).length
      return n === 0 ? "" : (n === 1 ? "Sent " + r.sent[0].name : "Sent " + n + " files")
    }, function () { root.refresh() })
  }

  function sendScreenshot() {
    return act(["screenshot"], "screenshot", function (r) { return "Sent " + r.sent[0].name }, function () { root.refresh() })
  }

  function sendUrl(url, format) {
    var args = ["web", url]
    if (format) args = args.concat(["--format", format])
    return act(args, "web", function (r) { return "Sent " + (r.name || "the page") }, function () { root.refresh() })
  }

  function sendClipboardUrl() {
    return act(["web", "clipboard"], "web", function (r) { return "Sent " + (r.name || "the page") }, function () { root.refresh() })
  }

  function sendAgenda() {
    return act(["agenda", "send"], "agenda", function (r) { return r.skipped ? "Today's agenda is already there" : "Sent " + (r.name || "today's agenda") }, function () { root.refresh() })
  }

  function setSetting(key, value) {
    var raw = (typeof value === "object") ? JSON.stringify(value) : String(value)
    var copy = Object.assign({}, settings)
    copy[key] = value
    settings = copy
    run(["settings", "set", key, raw], function (r) {
      if (!r.ok) root.say(r.error || "Couldn't save that setting", true)
      else root.settings = r.settings
    })
  }

  function openPath(path) {
    Quickshell.execDetached(["xdg-open", path])
  }

  // ------------------------------------------------------------------- IPC

  //   omarchy-shell remarkable toggle
  //   omarchy-shell remarkable sendScreenshot
  //   omarchy-shell remarkable sendClipboardUrl
  //   omarchy-shell remarkable send /path/to/file.pdf
  property IpcHandler ipc: IpcHandler {
    target: "remarkable"

    function open(): string { root.openWindow({}); return "ok" }
    function toggle(): string { root.toggleWindow(); return "ok" }
    function search(query: string): string { root.openWindow({ page: "search", query: query }); return "ok" }
    function view(id: string): string { root.openWindow({ page: "viewer", id: id }); return "ok" }
    function refresh(): string { root.refresh(); return "ok" }
    function send(path: string): string { root.sendFiles([path]); return "sending" }
    function sendScreenshot(): string { root.sendScreenshot(); return "sending" }
    function sendClipboardUrl(): string { root.sendClipboardUrl(); return "sending" }
    function sendAgenda(): string { root.sendAgenda(); return "sending" }
    function status(): string {
      return JSON.stringify({
        daemon: root.daemonUp, paired: root.paired, documents: root.documents.length,
        lastSync: root.lastSync, usb: root.usb, ssh: root.ssh, device: root.device,
        printer: root.printer, job: root.job
      })
    }
  }

  Component.onDestruction: {
    if (daemon.running) daemon.write("quit\n")
  }
}
