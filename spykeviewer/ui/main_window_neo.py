import os
from collections import OrderedDict
import logging
import traceback
import inspect
import sys
import pickle
import copy

import neo
from neo.io.baseio import BaseIO

from PyQt4.QtCore import (Qt, pyqtSignature, QThread, QMutex,
                          SIGNAL, QUrl, QSettings)
from PyQt4.QtGui import (QFileSystemModel, QHeaderView, QListWidgetItem,
                         QMessageBox, QApplication, QProgressDialog,
                         QFileDialog, QDesktopServices)

from spykeutils.progress_indicator import ignores_cancel, CancelException
from spykeutils import SpykeException
from spykeutils.plugin.data_provider_neo import NeoDataProvider
from spykeutils.plugin.data_provider_stored import NeoStoredProvider
from spykeutils.plugin.analysis_plugin import AnalysisPlugin

from main_window import MainWindow
from settings import SettingsWindow
from filter_dialog import FilterDialog
from filter_group_dialog import FilterGroupDialog
from plugin_editor_dock import PluginEditorDock
from filter_dock import FilterDock
from plugin_model import PluginModel
from ..plugin_framework.data_provider_viewer import NeoViewerProvider


logger = logging.getLogger('spykeviewer')


#noinspection PyCallByClass,PyTypeChecker,PyArgumentList
class MainWindowNeo(MainWindow):
    """ Implements Neo functionality in the main window
    """

    def __init__(self):
        super(MainWindowNeo, self).__init__()

        self.file_system_model = None
        self.block_ids = {}
        self.block_names = OrderedDict()  # Just for the display order
        self.block_files = {}
        self.channel_group_names = {}

        # Initialize filters
        settings = QSettings()
        if not settings.contains('filterPath'):
            data_path = QDesktopServices.storageLocation(
                QDesktopServices.DataLocation)
            self.filter_path = os.path.join(data_path, 'filters')
        else:
            self.filter_path = settings.value('filterPath')

        filter_types = [('Block', 'block'), ('Segment', 'segment'),
                        ('Recording Channel Group', 'rcg'),
                        ('Recording Channel', 'rc'),
                        ('Unit', 'unit')]
        self.filter_populate_function = \
            {'Block': self.populate_neo_block_list,
             'Recording Channel': self.populate_neo_channel_list,
             'Recording Channel Group': self.populate_neo_channel_group_list,
             'Segment': self.populate_neo_segment_list,
             'Unit': self.populate_neo_unit_list}

        self.filterDock = FilterDock(self.filter_path, filter_types,
                                     menu=self.menuFilter, parent=self)
        self.filterDock.setObjectName('filterDock')
        self.filterDock.current_filter_changed.connect(
            self.on_current_filter_changed)
        self.filterDock.filters_changed.connect(
            self.on_filters_changed)
        self.addDockWidget(Qt.RightDockWidgetArea, self.filterDock)

        self.show_filter_exceptions = True

        # Initialize plugin system
        self.pluginEditorDock = PluginEditorDock()
        self.pluginEditorDock.setObjectName('editorDock')
        self.addDockWidget(Qt.RightDockWidgetArea, self.pluginEditorDock)
        self.pluginEditorDock.setVisible(False)
        self.pluginEditorDock.plugin_saved.connect(self.plugin_saved)
        self.pluginEditorDock.file_available.connect(self.file_available)

        self.consoleDock.edit_script = lambda (path): \
            self.pluginEditorDock.add_file(path)

        from spyderlib.utils.misc import get_error_match

        def p(x):
            match = get_error_match(unicode(x))
            if match:
                fname, lnb = match.groups()
                self.pluginEditorDock.show_position(fname, int(lnb))

        self.connect(self.console, SIGNAL("go_to_error(QString)"), p)

        # Initialize Neo navigation
        self.file_system_model = QFileSystemModel()
        self.file_system_model.setRootPath('')
        self.fileTreeView.setModel(self.file_system_model)
        self.fileTreeView.setCurrentIndex(
            self.file_system_model.index(self.dir))
        self.fileTreeView.expand(self.file_system_model.index(self.dir))

        self.fileTreeView.setColumnHidden(1, True)
        self.fileTreeView.setColumnHidden(2, True)
        self.fileTreeView.setColumnHidden(3, True)

        self.fileTreeView.header().setResizeMode(QHeaderView.ResizeToContents)

        self.activate_neo_mode()
        self._finish_initialization()

    def _finish_initialization(self):
        self.update_view_menu()
        self.restore_state()
        self.run_startup_script()
        self.reload_plugins()

        # Restore plugin configurations
        configs_path = os.path.join(self.data_path, 'plugin_configs.p')
        if os.path.isfile(configs_path):
            with open(configs_path, 'r') as f:
                try:
                    configs = pickle.load(f)
                    self.set_plugin_configs(configs)
                except:
                    pass  # It does not matter if we can't load plugin configs

    def load_current_selection(self):
        current_selection = os.path.join(
            self.selection_path, '.current.sel')
        if os.path.isfile(current_selection):
            self.load_selections_from_file(current_selection)
        else:
            self.populate_selection_menu()

    def activate_neo_mode(self):
        self.provider = NeoViewerProvider(self)
        self.provider_factory = NeoStoredProvider.from_current_selection
        self.console.interpreter.locals['current'] = self.provider
        if self.ipy_kernel:
            self.ipy_kernel.get_user_namespace()['current'] = self.provider

    def get_plugin_configs(self):
        """ Return dictionary indexed by (name,path) tuples with configuration
        dictionaries for all plugins.
        """
        indices = self.analysisModel.get_all_indices()
        c = {}

        for idx in indices:
            path = self.analysisModel.data(idx,
                                           self.analysisModel.FilePathRole)
            plug = self.analysisModel.data(idx, self.analysisModel.DataRole)
            if plug:
                c[(plug.get_name(), path)] = plug.get_parameters()

        return c

    def set_plugin_configs(self, configs):
        """ Takes a dictionary indexed by plugin name with configuration
        dictionaries for plugins and sets configurations of plugins.
        """
        indices = self.analysisModel.get_all_indices()

        d = {}
        for idx in indices:
            path = self.analysisModel.data(idx,
                                           self.analysisModel.FilePathRole)
            plug = self.analysisModel.data(idx, self.analysisModel.DataRole)
            if plug:
                d[(plug.get_name(), path)] = plug

        for n, c in configs.iteritems():
            if n in d:
                d[n].set_parameters(c)

    def reload_plugins(self, keep_configs=True):
        old_path = None
        old_configs = {}
        if hasattr(self, 'analysisModel'):
            if keep_configs:
                old_configs = self.get_plugin_configs()
            item = self.neoAnalysesTreeView.currentIndex()
            if item:
                old_path = self.analysisModel.data(item,
                                                   self.analysisModel.FilePathRole)

        try:
            self.analysisModel = PluginModel()
            for p in self.plugin_paths:
                self.analysisModel.add_path(p)
        except Exception, e:
            QMessageBox.critical(self, 'Error loading plugins', str(e))
            return

        self.neoAnalysesTreeView.setModel(self.analysisModel)

        selected_index = None
        if old_path:
            indices = self.analysisModel.get_indices_for_path(old_path)
            if indices:
                selected_index = indices[0]
                self.neoAnalysesTreeView.setCurrentIndex(selected_index)
        self.neoAnalysesTreeView.expandAll()
        self.neoAnalysesTreeView.selectionModel().currentChanged.connect(
            self.selected_analysis_changed)
        self.selected_analysis_changed(selected_index)
        self.set_plugin_configs(old_configs)
        self.reload_neo_io_plugins()

    def reload_neo_io_plugins(self):
        for pp in self.plugin_paths:
            for f in os.listdir(pp):
                p = os.path.join(pp, f)

                if os.path.isdir(p):
                    continue
                if not p.lower().endswith('io.py'):
                    continue

                exc_globals = {}
                try:
                    execfile(p, exc_globals)
                except Exception:
                    logger.warning('Error during execution of ' +
                                   'potential Neo IO file ' + p + ':\n' +
                                   traceback.format_exc() + '\n')

                for cl in exc_globals.values():
                    if not inspect.isclass(cl):
                        continue

                    # Should be a subclass of AnalysisPlugin...
                    if not issubclass(cl, BaseIO):
                        continue
                        # ...but should not be AnalysisPlugin (can happen
                    # when directly imported)
                    if cl == BaseIO:
                        continue

                    if not cl in neo.io.iolist:
                        neo.io.iolist.append(cl)

    def get_letter_id(self, id, small=False):
        """ Return a name consisting of letters given an integer
        """
        if id < 0:
            return ''

        name = ''
        id += 1
        if small:
            start = ord('a') - 1
        else:
            start = ord('A') - 1
        while id >= 1:
            name += str(chr(start + (id % 26)))
            id /= 26
        return name[::-1]

    class LoadWorker(QThread):
        def __init__(self, file_name, indices):
            QThread.__init__(self)
            self.file_name = file_name
            self.indices = indices
            self.blocks = []

        def run(self):
            self.blocks = NeoDataProvider.get_blocks(self.file_name, False)

    @ignores_cancel
    def load_file_callback(self):
        if not self.load_worker:
            self.progress.done()
            return

        # Load worker thread finished
        blocks = self.load_worker.blocks
        if blocks is None:
            logger.error('Could not read file "%s"' %
                         self.load_worker.file_name)

        for block in blocks:
            name = block.name
            if not name or name == 'One segment only':
                name = self.file_system_model.fileName(
                    self.load_worker.indices[0])
            name += ' (%s)' % self.get_letter_id(self.block_index)

            self.block_names[block] = name
            self.block_ids[block] = self.get_letter_id(self.block_index)
            self.block_files[block] = self.load_worker.file_name
            self.block_index += 1

        self.load_progress.reset()
        self.progress.step()

        # Create new load worker thread
        indices = self.load_worker.indices[1:]
        if not indices:
            self.progress.done()
            self.populate_neo_block_list()
            self.load_worker = None
            return

        f = indices[0]
        filepath = self.file_system_model.filePath(f)

        self.load_worker = self.LoadWorker(filepath, indices)
        self.load_progress.setLabelText(filepath)
        self.load_progress.show()
        self.load_worker.finished.connect(self.load_file_callback)
        self.load_worker.terminated.connect(self.load_file_callback)
        self.load_worker.start()

    def file_available(self, available):
        self.actionSavePlugin.setEnabled(available)
        self.actionSavePluginAs.setEnabled(available)

    @ignores_cancel
    def on_neoLoadFilesButton_pressed(self):
        self.neoBlockList.clear()
        self.block_ids.clear()
        self.block_files.clear()
        self.block_names.clear()

        indices = self.fileTreeView.selectedIndexes()
        self.block_index = 0
        self.progress.begin('Loading data files...')
        self.progress.set_ticks(len(indices))

        filepath = self.file_system_model.filePath(indices[0])

        self.load_worker = self.LoadWorker(filepath, indices)
        self.load_progress = QProgressDialog(self.progress)
        self.load_progress.setWindowTitle('Loading File')
        self.load_progress.setLabelText(filepath)
        self.load_progress.setMaximum(0)
        self.load_progress.setCancelButton(None)
        self.load_worker.finished.connect(self.load_file_callback)
        self.load_worker.terminated.connect(self.load_file_callback)
        self.load_progress.show()
        self.load_worker.start()

    def on_fileTreeView_doubleClicked(self, index):
        self.on_neoLoadFilesButton_pressed()

    def is_filtered(self, item, filters):
        """ Return if one of the filter functions in the given list
            applies to the given item. Combined filters are ignored.
        """
        for f, n in filters:
            if f.combined:
                continue
            try:
                if not f.function()(item):
                    return True
            except Exception, e:
                if self.show_filter_exceptions:
                    sys.stderr.write(
                        'Exception in filter ' + n + ':\n' + str(e) + '\n')
                if not f.on_exception:
                    return True
        return False

    def filter_list(self, items, filters):
        """ Return a filtered list of the given list with the given filter
            functions. Only combined filters are used.
        """
        if not items:
            return items
        item_type = type(items[0])
        for f, n in filters:
            if not f.combined:
                continue
            try:
                items = [i for i in f.function()(items)
                         if isinstance(i, item_type)]
            except Exception, e:
                if self.show_filter_exceptions:
                    sys.stderr.write(
                        'Exception in filter ' + n + ':\n' + str(e) + '\n')
                if not f.on_exception:
                    return []
        return items

    def refresh_neo_view(self):
        self.set_current_selection(self.provider.data_dict())

    def populate_neo_block_list(self):
        """ Fill the block list with appropriate entries.
            Qt.UserRole: The block itself
        """
        self.neoBlockList.clear()

        filters = self.filterDock.get_active_filters('Block')

        blocks = self.filter_list(self.block_names.keys(), filters)
        for b in blocks:
            if self.is_filtered(b, filters):
                continue

            item = QListWidgetItem(self.block_names[b])
            item.setData(Qt.UserRole, b)
            self.neoBlockList.addItem(item)

        self.neoBlockList.setCurrentRow(0)

    def neo_blocks(self):
        return [t.data(Qt.UserRole) for t in
                self.neoBlockList.selectedItems()]

    def all_neo_blocks(self):
        return self.block_names.keys()

    def neo_block_file_names(self):
        """ Return a dictionary of filenames, indexed by blocks
        """
        return self.block_files

    def populate_neo_segment_list(self):
        self.neoSegmentList.clear()

        filters = self.filterDock.get_active_filters('Segment')

        for item in self.neoBlockList.selectedItems():
            block = item.data(Qt.UserRole)

            segments = self.filter_list(block.segments, filters)
            for i, s in enumerate(segments):
                if self.is_filtered(s, filters):
                    continue

                if s.name:
                    name = s.name + ' (%s-%i)' % (self.block_ids[s.block], i)
                else:
                    name = '%s-%i' % (self.block_ids[s.block], i)

                new_item = QListWidgetItem(name)
                new_item.setData(Qt.UserRole, s)
                self.neoSegmentList.addItem(new_item)

        self.neoSegmentList.setCurrentRow(0)

    def neo_segments(self):
        return [t.data(Qt.UserRole) for t in
                self.neoSegmentList.selectedItems()]

    def populate_neo_channel_group_list(self):
        self.neoChannelGroupList.clear()
        self.channel_group_names.clear()

        filters = self.filterDock.get_active_filters('Recording Channel Group')

        for item in self.neoBlockList.selectedItems():
            block = item.data(Qt.UserRole)

            rcgs = self.filter_list(block.recordingchannelgroups, filters)
            for i, rcg in enumerate(rcgs):
                if self.is_filtered(rcg, filters):
                    continue

                self.channel_group_names[rcg] = '%s-%s' % (
                    self.block_ids[rcg.block], self.get_letter_id(i, True))
                if rcg.name:
                    name = rcg.name + ' (%s)' % self.channel_group_names[rcg]
                else:
                    name = self.channel_group_names[rcg]
                new_item = QListWidgetItem(name)
                new_item.setData(Qt.UserRole, rcg)
                self.neoChannelGroupList.addItem(new_item)

        self.neoChannelGroupList.setCurrentRow(0)

    def neo_channel_groups(self):
        return [t.data(Qt.UserRole) for t in
                self.neoChannelGroupList.selectedItems()]

    def populate_neo_unit_list(self):
        self.neoUnitList.clear()

        filters = self.filterDock.get_active_filters('Unit')

        for item in self.neoChannelGroupList.selectedItems():
            rcg = item.data(Qt.UserRole)

            units = self.filter_list(rcg.units, filters)
            for i, u in enumerate(units):
                if self.is_filtered(u, filters):
                    continue
                if u.name:
                    name = u.name + ' (%s-%d)' % \
                           (self.channel_group_names[rcg], i)
                else:
                    name = '%s-%d' % (self.channel_group_names[rcg], i)
                new_item = QListWidgetItem(name)
                new_item.setData(Qt.UserRole, u)
                self.neoUnitList.addItem(new_item)

    def neo_units(self):
        return [t.data(Qt.UserRole) for t in
                self.neoUnitList.selectedItems()]

    def populate_neo_channel_list(self):
        """ Fill the channel list with appropriate entries. There is only
            one entry for each channel index. Data slots:
            Qt.UserRole: The channel index
            Qt.UserRole+1: A list of channels with this index
        """
        self.neoChannelList.clear()

        filters = self.filterDock.get_active_filters(
            'Recording Channel')

        for item in self.neoChannelGroupList.selectedItems():
            channel_group = item.data(Qt.UserRole)

            rcs = self.filter_list(channel_group.recordingchannels, filters)
            for rc in rcs:
                if self.is_filtered(rc, filters):
                    continue

                identifier = '%s.%d' % \
                             (self.channel_group_names[channel_group],
                              rc.index)
                if rc.name:
                    name = rc.name + ' (%s)' % identifier
                else:
                    name = identifier
                new_item = QListWidgetItem(name)
                new_item.setData(Qt.UserRole, rc)
                new_item.setData(Qt.UserRole + 1, rc.index)
                self.neoChannelList.addItem(new_item)
                self.neoChannelList.setItemSelected(new_item, True)

    def neo_channels(self):
        return [t.data(Qt.UserRole) for t in
                self.neoChannelList.selectedItems()]

    def on_neoBlockList_itemSelectionChanged(self):
        self.populate_neo_channel_group_list()
        self.populate_neo_segment_list()

    def on_neoChannelGroupList_itemSelectionChanged(self):
        self.populate_neo_channel_list()
        self.populate_neo_unit_list()

    def on_neoBlockList_itemDoubleClicked(self, item):
        print item.data(Qt.UserRole).annotations

    def on_neoSegmentList_itemDoubleClicked(self, item):
        print item.data(Qt.UserRole).annotations

    def on_neoChannelGroupList_itemDoubleClicked(self, item):
        print item.data(Qt.UserRole).annotations

    def on_neoChannelList_itemDoubleClicked(self, item):
        print item.data(Qt.UserRole).annotations

    def on_neoUnitList_itemDoubleClicked(self, item):
        print item.data(Qt.UserRole).annotations

    def selected_analysis_changed(self, current):
        enabled = True
        if not current:
            enabled = False
        elif not self.analysisModel.data(current, Qt.UserRole):
            enabled = False

        self.actionRunPlugin.setEnabled(enabled)
        self.actionEditPlugin.setEnabled(enabled)
        self.actionConfigurePlugin.setEnabled(enabled)
        self.actionRemotePlugin.setEnabled(enabled)
        self.actionShowPluginFolder.setEnabled(enabled)

    def current_plugin(self):
        item = self.neoAnalysesTreeView.currentIndex()
        if not item:
            return None

        return self.analysisModel.data(item, self.analysisModel.DataRole)

    def current_plugin_path(self):
        item = self.neoAnalysesTreeView.currentIndex()
        if not item:
            return None

        return self.analysisModel.data(item, self.analysisModel.FilePathRole)

    def get_plugin(self, name):
        """ Get plugin with the given name. Raises a SpykeException if
            multiple plugins with this name exist. Returns None if no such
            plugin exists.
        """
        plugins = self.analysisModel.get_plugins_for_name(name)
        if not plugins:
            return None
        if len(plugins) > 1:
            raise SpykeException('Multiple plugins named "%s" exist!' % name)

        return plugins[0]

    def start_plugin(self, name):
        """ Start first plugin with given name and return result of start()
            method. Raises a SpykeException if not exactly one plugins with
            this name exist.
        """
        plugins = self.analysisModel.get_plugins_for_name(name)
        if not plugins:
            return None
        if len(plugins) > 1:
            raise SpykeException('Multiple plugins named "%s" exist!' % name)

        return self._run_plugin([0])

    @pyqtSignature("")
    def on_actionNewFilter_triggered(self):
        top = self.filterDock.current_filter_type()
        group = self.filterDock.current_filter_group()

        dialog = FilterDialog(self.filterDock.filter_group_dict(), type=top,
                              group=group, parent=self)
        while dialog.exec_():
            try:
                self.filterDock.add_filter(dialog.name(), dialog.group(),
                                           dialog.type(), dialog.code(),
                                           dialog.on_exception(),
                                           dialog.combined())
                break
            except ValueError as e:
                QMessageBox.critical(self, 'Error creating filter', str(e))

    @pyqtSignature("")
    def on_actionDeleteFilter_triggered(self):
        self.filterDock.delete_current_filter()

    @pyqtSignature("")
    def on_actionEditFilter_triggered(self):
        self.editFilter(False)

    @pyqtSignature("")
    def on_actionCopyFilter_triggered(self):
        self.editFilter(True)

    def on_current_filter_changed(self):
        enabled = self.filterDock.current_is_data_item()
        self.actionEditFilter.setEnabled(enabled)
        self.actionDeleteFilter.setEnabled(enabled)
        self.actionCopyFilter.setEnabled(enabled)

    def on_filters_changed(self, filter_type):
        self.filter_populate_function[filter_type]()

    def editFilter(self, copy_item):
        top = self.filterDock.current_filter_type()
        group = self.filterDock.current_filter_group()
        name = self.filterDock.current_name()
        item = self.filterDock.current_item()

        group_filters = None
        if not self.filterDock.is_current_group():
            dialog = FilterDialog(
                self.filterDock.filter_group_dict(), top, group, name,
                item.code, item.combined, item.on_exception, self)
        else:
            group_filters = self.filterDock.group_filters(top, name)
            dialog = FilterGroupDialog(top, name, item.exclusive, self)

        while dialog.exec_():
            if copy_item and name == dialog.name():
                QMessageBox.critical(
                    self, 'Error saving',
                    'Please select a different name for the copied element')
                continue
            try:
                if not copy_item and name != dialog.name():
                    self.filterDock.delete_item(top, name, group)
                if not self.filterDock.is_current_group():
                    self.filterDock.add_filter(
                        dialog.name(), dialog.group(), dialog.type(),
                        dialog.code(), dialog.on_exception(),
                        dialog.combined(), overwrite=True)
                else:
                    self.filterDock.add_filter_group(
                        dialog.name(), dialog.type(), dialog.exclusive(),
                        copy.deepcopy(group_filters), overwrite=True)
                break
            except ValueError as e:
                QMessageBox.critical(self, 'Error saving', str(e))

    @pyqtSignature("")
    def on_actionNewFilterGroup_triggered(self):
        top = self.filterDock.current_filter_type()

        dialog = FilterGroupDialog(top, parent=self)
        while dialog.exec_():
            try:
                self.filterDock.add_filter_group(dialog.name(), dialog.type(),
                                                 dialog.exclusive())
                break
            except ValueError as e:
                QMessageBox.critical(self, 'Error creating group', str(e))

    @pyqtSignature("")
    def on_actionClearCache_triggered(self):
        NeoDataProvider.clear()
        self.neoBlockList.clear()
        self.block_ids.clear()
        self.block_files.clear()
        self.block_names.clear()

        self.populate_neo_block_list()

    def closeEvent(self, event):
        """ Saves all filters and plugins before closing
        """
        if not self.pluginEditorDock.close_all():
            event.ignore()
        else:
            filter_path = os.path.join(self.data_path, 'filters')
            # Ensure that filters folder exists
            if not os.path.exists(filter_path):
                try:
                    os.makedirs(filter_path)
                except OSError:
                    QMessageBox.critical(self, 'Error',
                                         'Could not create filter directory!')
            self.filterDock.save()

            # Store plugin configurations
            configs = self.get_plugin_configs()
            configs_path = os.path.join(self.data_path, 'plugin_configs.p')
            with open(configs_path, 'w') as f:
                pickle.dump(configs, f)

            event.accept()
            super(MainWindowNeo, self).closeEvent(event)

            # Prevent lingering threads
            self.fileTreeView.setModel(None)
            del self.file_system_model
            self.neoAnalysesTreeView.setModel(None)
            del self.analysisModel

    def add_neo_selection(self, data):
        """ Adds a new neo selection provider with the given data
        """
        self.selections.append(NeoStoredProvider(data, self.progress))

    def set_neo_selection(self, data):
        """ Sets the current selection according to the given provider data
        """
        self.progress.begin('Loading selection data...')
        self.progress.set_ticks(len(data['blocks']))

        # Load blocks which are not currently displayed
        i = len(self.block_names)
        for b in data['blocks']:
            # File already loaded?
            if unicode(b[1]) in self.block_files.values():
                self.progress.step()
                continue

            QApplication.setOverrideCursor(Qt.WaitCursor)
            blocks = NeoDataProvider.get_blocks(b[1], False)
            QApplication.restoreOverrideCursor()
            if not blocks:
                logger.error('Could not read file "%s"' % b[1])
                self.progress.step()
                continue

            for block in blocks:
                name = block.name
                if not name or name == 'One segment only':
                    name = os.path.basename(b[1])
                name += ' (%s)' % self.get_letter_id(i)

                self.block_names[block] = name
                self.block_ids[block] = self.get_letter_id(i)
                self.block_files[block] = b[1]
                i += 1

            self.progress.step()

        self.progress.done()

        self.populate_neo_block_list()

        block_list = [NeoDataProvider.get_block(b[1], b[0], False)
                      for b in data['blocks']]
        rcg_list = [block_list[rcg[1]].recordingchannelgroups[rcg[0]]
                    for rcg in data['channel_groups']]

        # Select blocks
        for i in self.neoBlockList.findItems('*',
                                             Qt.MatchWrap | Qt.MatchWildcard):
            block = i.data(Qt.UserRole)
            t = [NeoDataProvider.block_indices[block],
                 self.block_files[block]]
            i.setSelected(t in data['blocks'])

        # Select segments
        for i in self.neoSegmentList.findItems('*',
                                               Qt.MatchWrap | Qt.MatchWildcard):
            segment = i.data(Qt.UserRole)
            if not segment.block in block_list:
                i.setSelected(False)
                continue

            seg_idx = segment.block.segments.index(segment)
            block_idx = block_list.index(segment.block)
            i.setSelected([seg_idx, block_idx] in data['segments'])

        # Select recording channel groups
        for i in self.neoChannelGroupList.findItems('*',
                                                    Qt.MatchWrap | Qt.MatchWildcard):
            rcg = i.data(Qt.UserRole)
            if not rcg.block in block_list:
                i.setSelected(False)
                continue

            rcg_idx = rcg.block.recordingchannelgroups.index(rcg)
            block_idx = block_list.index(rcg.block)
            i.setSelected([rcg_idx, block_idx] in data['channel_groups'])

        # Select channels
        rcg_set = set(rcg_list)
        for i in self.neoChannelList.findItems('*',
                                               Qt.MatchWrap | Qt.MatchWildcard):
            i.setSelected(False)
            channel = i.data(Qt.UserRole)
            if not set(channel.recordingchannelgroups).intersection(rcg_set):
                continue

            for rcg in channel.recordingchannelgroups:
                if [rcg.recordingchannels.index(channel),
                        rcg_list.index(rcg)] in data['channels']:
                    i.setSelected(True)
                    break

        # Select units
        for i in self.neoUnitList.findItems('*',
                                            Qt.MatchWrap | Qt.MatchWildcard):
            unit = i.data(Qt.UserRole)
            if unit.recordingchannelgroup not in rcg_list:
                i.setSelected(False)
                continue

            rcg_idx = rcg_list.index(unit.recordingchannelgroup)
            unit_idx = unit.recordingchannelgroup.units.index(unit)
            i.setSelected([unit_idx, rcg_idx] in data['units'])

    class SaveWorker(QThread):
        def __init__(self, file_name, blocks):
            QThread.__init__(self)
            self.file_name = file_name
            self.blocks = blocks
            self.io = None
            self.terminated.connect(self.cleanup)
            self.finished.connect(self.cleanup)

        def run(self):
            if self.file_name.endswith('.mat'):
                self.io = neo.io.NeoMatlabIO(filename=self.file_name)
                self.io.write_block(self.blocks[0])
            else:
                self.io = neo.io.NeoHdf5IO(filename=self.file_name)
                for block in self.blocks:
                    self.io.save(block)

        def cleanup(self):
            if self.io:
                if hasattr(self.io, 'close'):
                    self.io.close()
                self.io = None

    def _save_blocks(self, blocks, file_name, selected_filter):
        if not blocks:
            QMessageBox.warning(self, 'Cannot save data',
                                'No data to save found!')
            self.progress.done()
            return
        self.progress.set_ticks(0)
        self.progress.setWindowTitle('Writing data...')
        self.progress.set_status('')

        if not file_name.endswith('.h5') and not file_name.endswith('.mat'):
            if selected_filter.endswith('.mat)'):
                file_name += '.mat'
            else:
                file_name += '.h5'

        self.worker = self.SaveWorker(file_name, blocks)
        self.worker.finished.connect(self.progress.done)
        self.progress.canceled.connect(self.worker.terminate)
        self.worker.start()

    @pyqtSignature("")
    def on_actionSave_Data_triggered(self):
        d = QFileDialog(self, 'Choose where to save data')
        d.setAcceptMode(QFileDialog.AcceptSave)
        d.setNameFilters(['HDF5 files (*.h5)', 'Matlab files (*.mat)'])
        #d.setDefaultSuffix('h5')
        d.setConfirmOverwrite(True)
        if d.exec_():
            file_name = unicode(d.selectedFiles()[0])
        else:
            return

        self.progress.begin('Collecting data to save...')
        blocks = self.all_neo_blocks()
        self._save_blocks(blocks, file_name, d.selectedFilter())

    @pyqtSignature("")
    def on_actionSave_Selected_Data_triggered(self):
        d = QFileDialog(self, 'Choose where to save selected data')
        d.setAcceptMode(QFileDialog.AcceptSave)
        d.setNameFilters(['HDF5 files (*.h5)', 'Matlab files (*.mat)'])
        #d.setDefaultSuffix('h5')
        d.setConfirmOverwrite(True)
        if d.exec_():
            file_name = unicode(d.selectedFiles()[0])
        else:
            return

        self.progress.begin('Collecting data to save...')
        blocks = self.provider.selection_blocks()
        self._save_blocks(blocks, file_name, d.selectedFilter())

    def on_refreshAnalysesButton_pressed(self):
        self.reload_plugins()

    @pyqtSignature("")
    def on_actionSettings_triggered(self):
        settings = SettingsWindow(self.selection_path, self.filter_path,
                                  AnalysisPlugin.data_dir, self.remote_script, self.plugin_paths,
                                  self)

        if settings.exec_() == settings.Accepted:
            self.selection_path = settings.selection_path()
            self.filter_path = settings.filter_path()
            self.remote_script = settings.remote_script()
            self.plugin_paths = settings.plugin_paths()
            if self.plugin_paths:
                self.pluginEditorDock.set_default_path(self.plugin_paths[-1])
            self.reload_plugins()

    @pyqtSignature("")
    def on_actionRunPlugin_triggered(self):
        ana = self.current_plugin()
        if not ana:
            return

        self._run_plugin(ana)

    def _run_plugin(self, plugin):
        try:
            return plugin.start(self.provider, self.selections)
        except SpykeException, err:
            self.progress.done()
            QMessageBox.critical(self, 'Error executing analysis', str(err))
        except CancelException:
            return None
        except Exception, e:
            # Only print stack trace from plugin on
            tb = sys.exc_info()[2]
            while not ('self' in tb.tb_frame.f_locals and
                               tb.tb_frame.f_locals['self'] == plugin):
                tb = tb.tb_next
            traceback.print_exception(type(e), e, tb)
            return None

    def on_neoAnalysesTreeView_doubleClicked(self, index):
        self.on_actionRunPlugin_triggered()

    @pyqtSignature("")
    def on_actionEditPlugin_triggered(self):
        item = self.neoAnalysesTreeView.currentIndex()
        path = ''
        if item:
            path = self.analysisModel.data(item,
                                           self.analysisModel.FilePathRole)
        if not path and self.plugin_paths:
            path = self.plugin_paths[0]
        self.pluginEditorDock.add_file(path)

    @pyqtSignature("")
    def on_actionConfigurePlugin_triggered(self):
        ana = self.current_plugin()
        if not ana:
            return

        ana.configure()

    @pyqtSignature("")
    def on_actionRefreshPlugins_triggered(self):
        self.reload_plugins()

    @pyqtSignature("")
    def on_actionNewPlugin_triggered(self):
        self.pluginEditorDock.new_file()

    @pyqtSignature("")
    def on_actionSavePlugin_triggered(self):
        self.pluginEditorDock.save_current()

    @pyqtSignature("")
    def on_actionSavePluginAs_triggered(self):
        self.pluginEditorDock.save_current(True)

    @pyqtSignature("")
    def on_actionShowPluginFolder_triggered(self):
        QDesktopServices.openUrl(QUrl.fromLocalFile(
            os.path.dirname(self.current_plugin_path())))

    @pyqtSignature("")
    def on_actionRemotePlugin_triggered(self):
        import subprocess
        import pickle

        selections = self.serialize_selections()
        config = pickle.dumps(self.current_plugin().get_parameters())
        f = open(self.remote_script, 'r')
        code = f.read()
        subprocess.Popen(['python', '-c', '%s' % code,
                          type(self.current_plugin()).__name__,
                          self.current_plugin_path(),
                          selections, '-cf', '-c', config,
                          '-dd', AnalysisPlugin.data_dir])

    @pyqtSignature("")
    def on_actionEdit_Startup_Script_triggered(self):
        self.pluginEditorDock.add_file(self.startup_script)

    @pyqtSignature("")
    def on_actionRestorePluginConfigurations_triggered(self):
        self.reload_plugins(False)

    def on_neoAnalysesTreeView_customContextMenuRequested(self, pos):
        self.menuPlugins.popup(self.neoAnalysesTreeView.mapToGlobal(pos))

    def plugin_saved(self, path):
        if path == self.startup_script:
            return

        plugin_path = os.path.normpath(os.path.realpath(path))
        in_dirs = False
        for p in self.plugin_paths:
            dir = os.path.normpath(os.path.realpath(p))
            if os.path.commonprefix([plugin_path, dir]) == dir:
                in_dirs = True
                break

        if in_dirs:
            self.reload_plugins()
        else:
            if QMessageBox.question(self, 'Warning',
                                    'The file "%s"' % plugin_path +
                                    ' is not in the currently valid plugin '
                                    'directories. Do you want to open the '
                                    'directory'
                                    'settings now?',
                                    QMessageBox.Yes | QMessageBox.No) == \
                    QMessageBox.No:
                return
            self.on_actionSettings_triggered()
