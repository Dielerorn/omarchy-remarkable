.pragma library
// Nerd Font Material Design glyphs, by name (ryanoasis/nerd-fonts glyphnames.json).

var tablet = "\u{f04f6}"         // md-tablet
var library = "\u{f125f}"        // md-bookshelf
var search = "\u{f0349}"         // md-magnify
var send = "\u{f048a}"           // md-send
var notes = "\u{f039e}"          // md-note_text
var settings = "\u{f0493}"       // md-cog
var folder = "\u{f024b}"         // md-folder
var folderPlus = "\u{f0257}"     // md-folder_plus
var folderMove = "\u{f0252}"     // md-folder_move
var pdf = "\u{f0226}"            // md-file_pdf_box
var notebook = "\u{f082e}"       // md-notebook
var book = "\u{f14f7}"           // md-book_open_variant
var back = "\u{f004d}"           // md-arrow_left
var prev = "\u{f0141}"           // md-chevron_left
var next = "\u{f0142}"           // md-chevron_right
var refresh = "\u{f0450}"        // md-refresh
var upload = "\u{f0552}"         // md-upload
var download = "\u{f01da}"       // md-download
var printer = "\u{f042a}"        // md-printer
var web = "\u{f059f}"            // md-web
var calendar = "\u{f00f6}"       // md-calendar_today
var screenshot = "\u{f0e51}"     // md-monitor_screenshot
var usb = "\u{f0553}"            // md-usb
var battery = "\u{f0079}"        // md-battery
var charging = "\u{f0084}"       // md-battery_charging
var star = "\u{f04ce}"           // md-star
var trash = "\u{f0a7a}"          // md-trash_can_outline
var rename = "\u{f0455}"         // md-rename_box
var exportIcon = "\u{f0207}"     // md-export
var openExternal = "\u{f03cc}"   // md-open_in_new
var handwriting = "\u{f113d}"    // md-text_recognition
var pen = "\u{f0d13}"            // md-fountain_pen_tip
var terminal = "\u{f08a9}"       // md-console_network
var cloudOk = "\u{f0160}"        // md-cloud_check
var cloudOff = "\u{f0164}"       // md-cloud_off_outline
var cloudSync = "\u{f063f}"      // md-cloud_sync
var alert = "\u{f05d6}"          // md-alert_circle_outline
var check = "\u{f012c}"          // md-check
var close = "\u{f0156}"          // md-close
var plus = "\u{f0415}"           // md-plus
var eye = "\u{f0208}"            // md-eye
var document = "\u{f09ee}"       // md-file_document_outline
var image = "\u{f02e9}"          // md-image
var quote = "\u{f0757}"          // md-format_quote_open
var backup = "\u{f006f}"         // md-backup_restore
var cast = "\u{f0118}"           // md-cast
var markdown = "\u{f0354}"       // md-language_markdown
var clipboard = "\u{f014d}"      // md-clipboard_text
var wifi = "\u{f05a9}"           // md-wifi
var disk = "\u{f02ca}"           // md-harddisk
var home = "\u{f02dc}"           // md-home
var grid = "\u{f0570}"           // md-view_grid
var list = "\u{f0572}"           // md-view_list
var template = "\u{f0ece}"       // md-tablet_dashboard

function forType(type) {
  if (type === "pdf") return pdf
  if (type === "epub") return book
  return notebook
}
