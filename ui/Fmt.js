.pragma library

function ago(epochSeconds) {
  if (!epochSeconds) return ""
  var s = Math.max(0, Date.now() / 1000 - epochSeconds)
  if (s < 45) return "just now"
  if (s < 3600) return Math.round(s / 60) + " min ago"
  if (s < 86400) return Math.round(s / 3600) + " h ago"
  return Math.round(s / 86400) + " d ago"
}

// "2026-09-13T20:19:15Z" -> "13 Sep" / "13 Sep 2025" / "Today 20:19"
function modified(iso) {
  if (!iso) return ""
  var d = new Date(iso)
  if (isNaN(d.getTime())) return ""
  var now = new Date()
  var months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
  var sameDay = d.toDateString() === now.toDateString()
  if (sameDay) return "Today " + pad(d.getHours()) + ":" + pad(d.getMinutes())
  var label = d.getDate() + " " + months[d.getMonth()]
  return d.getFullYear() === now.getFullYear() ? label : label + " " + d.getFullYear()
}

function pad(n) { return n < 10 ? "0" + n : "" + n }

function jobLabel(job) {
  var labels = {
    "mirror": "Updating the local copy…", "index": "Updating search…", "notes": "Updating notes…",
    "transcribe": "Reading handwriting…", "sendfolder": "Sending files…", "usb-backup": "Backing up over USB…",
    "templates": "Copying templates…", "agenda": "Sending the agenda…"
  }
  return labels[job] || "Working…"
}

function bytes(n) {
  if (n === undefined || n === null) return ""
  var units = ["B", "KB", "MB", "GB", "TB"]
  var i = 0
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++ }
  return (i === 0 ? n : n.toFixed(n < 10 ? 1 : 0)) + " " + units[i]
}

function typeLabel(t) {
  if (t === "pdf") return "PDF"
  if (t === "epub") return "EPUB"
  if (t === "notebook") return "Notebook"
  return ""
}

// FTS snippet markers to rich text.
function snippet(s) {
  var esc = String(s || "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
  return esc.replace(/‹/g, "<b>").replace(/›/g, "</b>").replace(/&lt;b&gt;/g, "<b>").replace(/&lt;\/b&gt;/g, "</b>")
}
