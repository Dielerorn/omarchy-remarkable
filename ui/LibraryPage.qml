import QtQuick
import QtQuick.Layouts
import qs.Commons
import qs.Commons as Commons
import qs.Ui
import "Glyphs.js" as G
import "Fmt.js" as Fmt

Item {
  id: root

  property var service: null
  property string folderId: ""
  property string sortMode: "recent"
  signal folderChosen(string id)
  signal documentChosen(string id)

  readonly property color fg: Commons.Color.foreground
  readonly property color muted: Util.alpha(fg, 0.62)

  readonly property var items: computeItems()

  function computeItems() {
    if (!service) return []
    var _ = service.libraryVersion
    var q = filter.text.toLowerCase().trim()
    var byName = function (a, b) { return String(a.name).localeCompare(String(b.name), undefined, { numeric: true }) }
    var byRecent = function (a, b) { return String(b.modified).localeCompare(String(a.modified)) }
    var docSort = sortMode === "name" ? byName : byRecent
    if (q) {
      return service.documents.filter(function (e) {
        return String(e.name).toLowerCase().indexOf(q) >= 0 || String(e.path).toLowerCase().indexOf(q) >= 0
      }).sort(docSort)
    }
    var kids = service.childrenOf(folderId)
    var dirs = kids.filter(function (e) { return e.folder }).sort(byName)
    var docs = kids.filter(function (e) { return !e.folder }).sort(docSort)
    return dirs.concat(docs)
  }

  function crumbs() {
    var out = []
    if (!service) return out
    var _ = service.libraryVersion
    var id = folderId
    var guard = 0
    while (id && guard++ < 32) {
      var e = service.entryById(id)
      if (!e) break
      out.unshift({ id: e.id, name: e.name })
      id = e.parent
    }
    return out
  }

  function childCount(id) {
    if (!service) return 0
    return service.childrenOf(id).length
  }

  // --------------------------------------------------------------- header
  ColumnLayout {
    anchors.fill: parent
    anchors.margins: Style.spacing.huge
    spacing: Style.spacing.xl

    RowLayout {
      Layout.fillWidth: true
      spacing: Style.spacing.lg

      // Breadcrumb
      Flow {
        Layout.fillWidth: true
        spacing: Style.spacing.sm
        Crumb { text: "My files"; current: root.folderId === ""; onClicked: root.folderChosen("") }
        Repeater {
          model: root.crumbs()
          delegate: Row {
            required property var modelData
            spacing: Style.spacing.sm
            Text { text: G.next; color: root.muted; font.family: Style.font.family; font.pixelSize: Style.font.body; anchors.verticalCenter: parent.verticalCenter }
            Crumb { text: modelData.name; current: modelData.id === root.folderId; onClicked: root.folderChosen(modelData.id) }
          }
        }
      }

      TextField {
        id: filter
        Layout.preferredWidth: Style.space(220)
        placeholderText: "Filter by name"
      }
      ButtonGroup {
        options: [{ value: "recent", label: "Recent" }, { value: "name", label: "Name" }]
        value: root.sortMode
        accent: Commons.Color.accent
        onChanged: function (v) { root.sortMode = v }
      }
      PanelActionButton {
        iconText: G.folderPlus
        tooltipText: "New folder here"
        onClicked: newFolderDialog.open()
      }
      PanelActionButton {
        iconText: G.upload
        tooltipText: "Send files to this folder"
        onClicked: if (root.service) root.service.pickAndSend(root.service.entryById(root.folderId) ? String(root.service.entryById(root.folderId).path).replace(/^\//, "") : "")
      }
      PanelActionButton {
        iconText: G.refresh
        tooltipText: "Check the cloud now"
        onClicked: if (root.service) root.service.refresh()
      }
    }

    // ------------------------------------------------------------ states
    Text {
      Layout.fillWidth: true
      visible: !!root.service && !root.service.paired
      text: "Not connected to the reMarkable cloud yet. Open the Device page to connect."
      color: root.muted
      font.family: Style.font.family
      font.pixelSize: Style.font.body
      wrapMode: Text.WordWrap
    }
    Text {
      Layout.fillWidth: true
      visible: !!root.service && root.service.paired && root.items.length === 0
      text: filter.text ? "Nothing matches “" + filter.text + "”." : "This folder is empty. Drop files here to send them."
      color: root.muted
      font.family: Style.font.family
      font.pixelSize: Style.font.body
    }

    // ---------------------------------------- folders strip, then documents
    Flickable {
      id: scroller
      Layout.fillWidth: true
      Layout.fillHeight: true
      clip: true
      contentHeight: stack.implicitHeight
      boundsBehavior: Flickable.StopAtBounds

      Column {
        id: stack
        width: scroller.width
        spacing: Style.spacing.xl

        Flow {
          width: parent.width
          spacing: Style.spacing.md
          visible: folderRepeater.count > 0
          Repeater {
            id: folderRepeater
            model: root.items.filter(function (e) { return e.folder })
            delegate: FolderTile {
              required property var modelData
              entry: modelData
              count: root.childCount(modelData.id)
              width: Math.floor((stack.width - Style.spacing.md * (Math.max(1, Math.floor(stack.width / Style.space(230))) - 1)) / Math.max(1, Math.floor(stack.width / Style.space(230))))
              onActivated: root.folderChosen(modelData.id)
              onMenuRequested: function (x, y) { var p = mapToItem(root, x, y); menu.openFor(modelData, p.x, p.y) }
            }
          }
        }

        Grid {
          id: grid
          width: parent.width
          readonly property int cols: Math.max(2, Math.floor(width / Style.space(178)))
          readonly property real cellWidth: Math.floor(width / cols)
          columns: cols
          Repeater {
            model: root.items.filter(function (e) { return !e.folder })
            delegate: DocCard {
              required property var modelData
              width: grid.cellWidth
              height: Math.round(grid.cellWidth * 1.33) + Style.space(48)
              service: root.service
              entry: modelData
              onActivated: root.documentChosen(modelData.id)
              onMenuRequested: function (x, y) { var p = mapToItem(root, x, y); menu.openFor(modelData, p.x, p.y) }
            }
          }
        }
      }
    }
  }

  component FolderTile: Rectangle {
    id: tile
    property var entry: ({})
    property int count: 0
    signal activated()
    signal menuRequested(real x, real y)
    height: Style.space(52)
    radius: Style.cornerRadius
    color: tileMouse.containsMouse ? Util.alpha(Commons.Color.accent, 0.16) : Util.alpha(Commons.Color.foreground, 0.045)
    border.width: 1
    border.color: Util.alpha(Commons.Color.foreground, 0.08)
    RowLayout {
      anchors.fill: parent
      anchors.leftMargin: Style.spacing.xl
      anchors.rightMargin: Style.spacing.xl
      spacing: Style.spacing.lg
      Text { text: G.folder; color: Commons.Color.accent; font.family: Style.font.family; font.pixelSize: Style.font.iconLarge + 4 }
      ColumnLayout {
        Layout.fillWidth: true
        spacing: 0
        Text { Layout.fillWidth: true; text: tile.entry.name || ""; color: Commons.Color.foreground; font.family: Style.font.family; font.pixelSize: Style.font.body; elide: Text.ElideRight }
        Text { text: tile.count === 1 ? "1 item" : tile.count + " items"; color: Util.alpha(Commons.Color.foreground, 0.55); font.family: Style.font.family; font.pixelSize: Style.font.caption }
      }
    }
    MouseArea {
      id: tileMouse
      anchors.fill: parent
      hoverEnabled: true
      acceptedButtons: Qt.LeftButton | Qt.RightButton
      cursorShape: Qt.PointingHandCursor
      onClicked: function (ev) { ev.button === Qt.RightButton ? tile.menuRequested(ev.x, ev.y) : tile.activated() }
    }
  }

  // ------------------------------------------------------------- context menu
  ContextMenu {
    id: menu
    service: root.service
    onRenameRequested: function (e) { renameDialog.target = e; renameField.text = e.name; renameDialog.open() }
    onMoveRequested: function (e) { moveDialog.target = e; moveDialog.choice = e.parent || ""; moveDialog.open() }
    onTrashRequested: function (e) { trashDialog.target = e; trashDialog.open() }
    onOpenRequested: function (e) { e.folder ? root.folderChosen(e.id) : root.documentChosen(e.id) }
  }

  ModalDialog {
    id: renameDialog
    property var target: null
    title: "Rename " + (target ? (target.folder ? "folder" : "document") : "")
    confirmText: "Rename"
    confirmEnabled: renameField.text.trim().length > 0
    TextField { id: renameField; Layout.fillWidth: true; onAccepted: renameDialog.confirmed() }
    onConfirmed: {
      renameDialog.close()
      if (target && renameField.text.trim() && renameField.text !== target.name)
        root.service.act(["doc", "rename", target.id, renameField.text.trim()], "rename", "Renamed to " + renameField.text.trim(), function () { root.service.refresh() })
    }
  }

  ModalDialog {
    id: moveDialog
    property var target: null
    property string choice: ""
    title: "Move " + (target ? "“" + target.name + "”" : "")
    confirmText: "Move"
    FolderPicker {
      Layout.fillWidth: true
      Layout.preferredHeight: Style.space(260)
      service: root.service
      selected: moveDialog.choice
      exclude: moveDialog.target && moveDialog.target.folder ? moveDialog.target.id : ""
      onPicked: function (id) { moveDialog.choice = id }
    }
    onConfirmed: {
      if (!target) return
      var dest = moveDialog.choice ? String(root.service.entryById(moveDialog.choice).path).replace(/^\//, "") : ""
      root.service.act(["doc", "move", target.id, dest], "move", "Moved to " + (dest || "My files"), function () { root.service.refresh() })
    }
  }

  ModalDialog {
    id: trashDialog
    property var target: null
    title: "Move “" + (target ? target.name : "") + "” to the trash?"
    confirmText: "Move to trash"
    destructive: true
    Text {
      text: "It goes to the reMarkable's own trash, where you can restore it on the tablet."
      color: Commons.Color.popups.text
      font.family: Style.font.family
      font.pixelSize: Style.font.body
      wrapMode: Text.WordWrap
      Layout.fillWidth: true
    }
    onConfirmed: if (target) root.service.act(["doc", "trash", target.id], "trash", "Moved to trash", function () { root.service.refresh() })
  }

  ModalDialog {
    id: newFolderDialog
    title: "New folder in " + (root.folderId && root.service && root.service.entryById(root.folderId) ? root.service.entryById(root.folderId).name : "My files")
    confirmText: "Create"
    confirmEnabled: folderField.text.trim().length > 0
    TextField { id: folderField; Layout.fillWidth: true; placeholderText: "Folder name"; onAccepted: newFolderDialog.confirmed() }
    onConfirmed: {
      newFolderDialog.close()
      var parentPath = root.folderId && root.service.entryById(root.folderId) ? String(root.service.entryById(root.folderId).path).replace(/^\//, "") + "/" : ""
      var name = folderField.text.trim()
      folderField.text = ""
      if (name) root.service.act(["folder", "create", parentPath + name], "mkdir", "Created " + name, function () { root.service.refresh() })
    }
  }

  component Crumb: Text {
    property bool current: false
    signal clicked()
    color: current ? Commons.Color.foreground : Util.alpha(Commons.Color.foreground, 0.62)
    font.family: Style.font.family
    font.pixelSize: Style.font.heading
    font.bold: current
    MouseArea {
      anchors.fill: parent
      cursorShape: parent.current ? Qt.ArrowCursor : Qt.PointingHandCursor
      onClicked: parent.clicked()
    }
  }
}
