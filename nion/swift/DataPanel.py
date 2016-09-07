# standard libraries
import copy
import functools
import gettext
import logging
import threading
import uuid
import weakref

# third party libraries
# None

# local libraries
from nion.swift import Decorators
from nion.swift import Panel
from nion.swift import Thumbnails
from nion.swift.model import DataGroup
from nion.swift.model import DataItem
from nion.ui import CanvasItem
from nion.ui import GridCanvasItem
from nion.ui import ListCanvasItem
from nion.ui import Widgets
from nion.utils import Event
from nion.utils import Geometry

_ = gettext.gettext


"""
    The data panel has two parts:

    (1) a selection of what collection is displayed, which may be a data group, smart data group,
    or the whole document. If the whole document, then an optional filter may also be applied.
    "within the last 24 hours" would be an example filter.

    (2) the list of data items from the collection. the user may further refine the list of
    items by filtering by additional criteria. the user also chooses the sorting on the list of
    data items.

"""


class DisplayItem(object):
    """ Provide a simplified interface to a data item for the purpose of display.

        The data_item property is always valid.

        There is typically one display item associated with each data item.

        Provides the following interface:
            (method) close()
            (property, read-only) data_item
            (property, read-only) title_str
            (property, read-only) datetime_str
            (property, read-only) format_str
            (property, read-only) status_str
            (method) draw_thumbnail(dispatch_task, ui, drawing_context, draw_rect)
            (method) get_mime_data(ui)
            (method) drag_started(ui, x, y, modifiers), returns mime_data, thumbnail_data
            (event) needs_update_event
    """

    def __init__(self, data_item, dispatch_task, ui):
        self.__data_item = data_item
        self.__dispatch_task = dispatch_task
        self.ui = ui
        self.needs_update_event = Event.Event()

        def data_item_content_changed(changes):
            self.needs_update_event.fire()

        self.__data_item_content_changed_event_listener = data_item.data_item_content_changed_event.listen(data_item_content_changed)

        self.__thumbnail_updated_event_listener = None
        self.__thumbnail_source = None

    def close(self):
        # remove the listener.
        if self.__thumbnail_updated_event_listener:
            self.__thumbnail_updated_event_listener.close()
            self.__thumbnail_updated_event_listener = None
        if self.__thumbnail_source:
            self.__thumbnail_source.close()
            self.__thumbnail_source = None
        self.__data_item_content_changed_event_listener.close()
        self.__data_item_content_changed_event_listener = None

    @property
    def data_item(self):
        return self.__data_item

    def __create_thumbnail_source(self):
        # grab the display specifier and if there is a display, handle thumbnail updating.
        display_specifier = self.__data_item.primary_display_specifier
        display = display_specifier.display
        if display and not self.__thumbnail_source:
            self.__thumbnail_source = Thumbnails.ThumbnailManager().thumbnail_source_for_display(self.__dispatch_task, self.ui, display)

            def thumbnail_updated():
                self.needs_update_event.fire()

            self.__thumbnail_updated_event_listener = self.__thumbnail_source.thumbnail_updated_event.listen(thumbnail_updated)

    def draw_thumbnail(self, dispatch_task, ui, draw_rect):
        drawing_context = ui.create_offscreen_drawing_context()
        display = self.__data_item.primary_display_specifier.display
        if display:
            self.__create_thumbnail_source()
            thumbnail_data = self.__thumbnail_source.thumbnail_data
            if thumbnail_data is not None:
                draw_rect = Geometry.fit_to_size(draw_rect, thumbnail_data.shape)
                drawing_context.draw_image(thumbnail_data, draw_rect[0][1], draw_rect[0][0], draw_rect[1][1], draw_rect[1][0])
        return drawing_context

    @property
    def title_str(self):
        data_item = self.__data_item
        return data_item.displayed_title

    @property
    def format_str(self):
        data_item = self.__data_item
        display_specifier = data_item.primary_display_specifier
        buffered_data_source = display_specifier.buffered_data_source
        return buffered_data_source.size_and_data_format_as_string if buffered_data_source else str()

    @property
    def datetime_str(self):
        data_item = self.__data_item
        return data_item.date_for_sorting_local_as_string

    @property
    def status_str(self):
        data_item = self.__data_item
        if data_item.is_live:
            display_specifier = data_item.primary_display_specifier
            buffered_data_source = display_specifier.buffered_data_source
            if buffered_data_source:
                live_metadata = buffered_data_source.metadata.get("hardware_source", dict())
                frame_index_str = str(live_metadata.get("frame_index", str()))
                partial_str = "{0:d}/{1:d}".format(live_metadata.get("valid_rows"), buffered_data_source.dimensional_shape[0]) if "valid_rows" in live_metadata else str()
                return "{0:s} {1:s} {2:s}".format(_("Live"), frame_index_str, partial_str)
        return str()

    def get_mime_data(self, ui):
        data_item = self.__data_item
        mime_data = ui.create_mime_data()
        mime_data.set_data_as_string("text/data_item_uuid", str(data_item.uuid))
        return mime_data

    def drag_started(self, ui, x, y, modifiers):
        data_item = self.__data_item
        mime_data = self.get_mime_data(ui)
        display_specifier = data_item.primary_display_specifier
        display = display_specifier.display
        self.__create_thumbnail_source()
        thumbnail_data = self.__thumbnail_source.thumbnail_data if self.__thumbnail_source else None
        return mime_data, thumbnail_data


class DataListController:
    """Control a list of display items in a list widget.

    The following properties are available:
        selected_indexes (r/o)

    The following methods can be called:
        close()
        periodic()
        set_selected_index(index)
        display_item_inserted(display_item, before_index) - ui thread
        display_item_removed(index) - ui thread

    The controller provides the following callbacks:
        on_delete_display_items(display_items)
        on_selection_changed(display_items)
        on_display_item_double_clicked(display_item)
        on_focus_changed(focused)
        on_context_menu_event(display_item, x, y, gx, gy)

    Display items should respond to these properties and methods and events:
        (method) close()
        (property, read-only) title_str
        (property, read-only) datetime_str
        (property, read-only) format_str
        (property, read-only) status_str
        (method) draw_thumbnail(dispatch_task, ui, draw_rect)
        (method) get_mime_data(ui)
        (method) drag_started(ui, x, y, modifiers), returns mime_data, thumbnail_data
    """

    def __init__(self, dispatch_task, add_task, clear_task, ui, selection):
        super().__init__()
        self.dispatch_task = dispatch_task
        self.__add_task = add_task
        self.__clear_task = clear_task
        self.ui = ui
        self.on_delete_display_items = None

        self.__display_items = list()
        self.__display_item_needs_update_listeners = list()

        class ListCanvasItemDelegate(object):
            def __init__(self, data_list_controller):
                self.__data_list_controller = data_list_controller

            @property
            def item_count(self):
                return self.__data_list_controller.display_item_count

            def on_context_menu_event(self, index, x, y, gx, gy):
                return self.__data_list_controller.context_menu_event(index, x, y, gx, gy)

            def on_delete_pressed(self):
                self.__data_list_controller._delete_pressed()

            def on_drag_started(self, index, x, y, modifiers):
                self.__data_list_controller.drag_started(index, x, y, modifiers)

            def paint_item(self, drawing_context, index, rect, is_selected):
                self.__data_list_controller.paint_item(drawing_context, index, rect, is_selected)

        self.__list_canvas_item = ListCanvasItem.ListCanvasItem(ListCanvasItemDelegate(self), selection)
        def focus_changed(focused):
            self.__list_canvas_item.update()
            if self.on_focus_changed:
                self.on_focus_changed(focused)
        self.__list_canvas_item.on_focus_changed = focus_changed
        self.scroll_area_canvas_item = CanvasItem.ScrollAreaCanvasItem(self.__list_canvas_item)
        self.scroll_area_canvas_item.auto_resize_contents = True
        self.scroll_bar_canvas_item = CanvasItem.ScrollBarCanvasItem(self.scroll_area_canvas_item)
        self.scroll_group_canvas_item = CanvasItem.CanvasItemComposition()
        self.scroll_group_canvas_item.layout = CanvasItem.CanvasItemRowLayout()
        self.scroll_group_canvas_item.add_canvas_item(self.scroll_area_canvas_item)
        self.scroll_group_canvas_item.add_canvas_item(self.scroll_bar_canvas_item)
        self.canvas_item = self.scroll_group_canvas_item
        self.selection = selection
        def selection_changed():
            self.selected_indexes = list(self.selection.indexes)
            if self.on_selection_changed:
                self.on_selection_changed([self.__display_items[index] for index in list(self.selection.indexes)])
        self.__selection_changed_listener = self.selection.changed_event.listen(selection_changed)
        self.selected_indexes = list()
        self.on_selection_changed = None
        self.on_context_menu_event = None
        self.on_focus_changed = None
        self.on_drag_started = None

        # changed data items keep track of items whose content has changed
        # the content changed messages may come from a thread so have to be
        # moved to the main thread via this object.
        self.__changed_display_items = False
        self.__changed_display_items_mutex = threading.RLock()

    def close(self):
        self.__clear_task(str(id(self)))
        self.__selection_changed_listener.close()
        self.__selection_changed_listener = None
        for display_item_needs_update_listener in self.__display_item_needs_update_listeners:
            display_item_needs_update_listener.close()
        self.__display_item_needs_update_listeners = None
        self.__display_items = None
        self.on_selection_changed = None
        self.on_context_menu_event = None
        self.on_drag_started = None
        self.on_focus_changed = None
        self.on_delete_display_items = None

    def __update_display_items(self):
        # handle the 'changed' stuff. a call to this function is scheduled
        # whenever __changed_display_items changes.
        with self.__changed_display_items_mutex:
            changed_display_items = self.__changed_display_items
            self.__changed_display_items = False
        if changed_display_items:
            self.__list_canvas_item.update()

    def set_selected_index(self, index):
        self.selection.set(index)

    def make_selection_visible(self):
        self.__list_canvas_item.make_selection_visible()

    # this message comes from the canvas item when delete key is pressed
    def _delete_pressed(self):
        if self.on_delete_display_items:
            self.on_delete_display_items([self.__display_items[index] for index in self.selection.indexes])

    @property
    def display_item_count(self):
        return len(self.__display_items)

    def _test_get_display_item(self, index):
        return self.__display_items[index]

    def context_menu_event(self, index, x, y, gx, gy):
        if self.on_context_menu_event:
            display_item = self.__display_items[index] if index is not None else None
            return self.on_context_menu_event(display_item, x, y, gx, gy)
        return False

    def drag_started(self, index, x, y, modifiers):
        mime_data, thumbnail_data = self.__display_items[index].drag_started(self.ui, x, y, modifiers)
        if mime_data:
            if self.on_drag_started:
                self.on_drag_started(mime_data, thumbnail_data)

    def __display_item_needs_update(self):
        with self.__changed_display_items_mutex:
            self.__changed_display_items = True
            self.__add_task(str(id(self)), self.__update_display_items)

    # call this method to insert a display item
    # not thread safe
    def display_item_inserted(self, display_item, before_index):
        self.__display_items.insert(before_index, display_item)
        self.__display_item_needs_update_listeners.insert(before_index, display_item.needs_update_event.listen(self.__display_item_needs_update))
        # tell the icon view to update.
        self.__list_canvas_item.update()

    # call this method to remove a display item (by index)
    # not thread safe
    def display_item_removed(self, index):
        self.__display_item_needs_update_listeners[index].close()
        del self.__display_item_needs_update_listeners[index]
        del self.__display_items[index]
        self.__list_canvas_item.update()

    # this message comes from the styled item delegate
    def paint_item(self, drawing_context, index, rect, is_selected):
        display_item = self.__display_items[index]
        with drawing_context.saver():
            draw_rect = ((rect[0][0] + 4, rect[0][1] + 4), (72, 72))
            drawing_context.add(display_item.draw_thumbnail(self.dispatch_task, self.ui, draw_rect))
            drawing_context.fill_style = "#000"
            drawing_context.font = "11px italic"
            drawing_context.fill_text(display_item.title_str, rect[0][1] + 4 + 72 + 4, rect[0][0] + 4 + 12)
            drawing_context.fill_text(display_item.format_str, rect[0][1] + 4 + 72 + 4, rect[0][0] + 4 + 12 + 15)
            drawing_context.fill_text(display_item.datetime_str, rect[0][1] + 4 + 72 + 4, rect[0][0] + 4 + 12 + 15 + 15)
            drawing_context.fill_text(display_item.status_str, rect[0][1] + 4 + 72 + 4, rect[0][0] + 4 + 12 + 15 + 15 + 15)


class DataGridController:
    """Control a grid of display items in a grid widget.

    The following properties are available:
        selected_indexes (r/o)

    The following methods can be called:
        close()
        periodic()
        set_selected_index(index)
        display_item_inserted(display_item, before_index) - ui thread
        display_item_removed(index) - ui thread

    The controller provides the following callbacks:
        on_delete_display_items(display_items)
        on_selection_changed(display_items)
        on_display_item_double_clicked(display_item)
        on_focus_changed(focused)
        on_context_menu_event(display_item, x, y, gx, gy)
        on_drag_started(mime_data, thumbnail_data)

    Display items should respond to these properties and methods and events:
        (method) close()
        (property, read-only) thumbnail
        (property, read-only) title_str
        (property, read-only) datetime_str
        (property, read-only) format_str
        (property, read-only) status_str
        (method) draw_thumbnail(dispatch_task, ui, draw_rect)
        (method) get_mime_data(ui)
        (method) drag_started(ui, x, y, modifiers), returns mime_data, thumbnail_data
    """

    def __init__(self, dispatch_task, add_task, clear_task, ui, selection):
        super(DataGridController, self).__init__()
        self.dispatch_task = dispatch_task
        self.__add_task = add_task
        self.__clear_task = clear_task
        self.ui = ui
        self.on_delete_display_items = None

        self.__display_items = list()
        self.__display_item_needs_update_listeners = list()

        class GridCanvasItemDelegate(object):
            def __init__(self, data_grid_controller):
                self.__data_grid_controller = data_grid_controller

            @property
            def item_count(self):
                return self.__data_grid_controller.display_item_count

            def paint_item(self, drawing_context, index, rect, is_selected):
                self.__data_grid_controller.paint_item(drawing_context, index, rect, is_selected)

            def on_context_menu_event(self, index, x, y, gx, gy):
                return self.__data_grid_controller.context_menu_event(index, x, y, gx, gy)

            def on_delete_pressed(self):
                self.__data_grid_controller._delete_pressed()

            def on_drag_started(self, index, x, y, modifiers):
                self.__data_grid_controller.drag_started(index, x, y, modifiers)

        self.icon_view_canvas_item = GridCanvasItem.GridCanvasItem(GridCanvasItemDelegate(self), selection)
        def icon_view_canvas_item_focus_changed(focused):
            self.icon_view_canvas_item.update()
            if self.on_focus_changed:
                self.on_focus_changed(focused)
        self.icon_view_canvas_item.on_focus_changed = icon_view_canvas_item_focus_changed
        self.scroll_area_canvas_item = CanvasItem.ScrollAreaCanvasItem(self.icon_view_canvas_item)
        self.scroll_area_canvas_item.auto_resize_contents = True
        self.scroll_bar_canvas_item = CanvasItem.ScrollBarCanvasItem(self.scroll_area_canvas_item)
        self.scroll_group_canvas_item = CanvasItem.CanvasItemComposition()
        self.scroll_group_canvas_item.layout = CanvasItem.CanvasItemRowLayout()
        self.scroll_group_canvas_item.add_canvas_item(self.scroll_area_canvas_item)
        self.scroll_group_canvas_item.add_canvas_item(self.scroll_bar_canvas_item)
        self.canvas_item = self.scroll_group_canvas_item
        self.selection = selection
        def selection_changed():
            self.selected_indexes = list(self.selection.indexes)
            if self.on_selection_changed:
                self.on_selection_changed([self.__display_items[index] for index in list(self.selection.indexes)])
        self.__selection_changed_listener = self.selection.changed_event.listen(selection_changed)
        self.selected_indexes = list()
        self.on_selection_changed = None
        self.on_context_menu_event = None
        self.on_focus_changed = None
        self.on_drag_started = None

        # changed data items keep track of items whose content has changed
        # the content changed messages may come from a thread so have to be
        # moved to the main thread via this object.
        self.__changed_display_items = False
        self.__changed_display_items_mutex = threading.RLock()
        self.__closed = False

    def close(self):
        assert not self.__closed
        self.__clear_task(str(id(self)))
        self.icon_view_canvas_item.detach_delegate()
        self.__selection_changed_listener.close()
        self.__selection_changed_listener = None
        for display_item_needs_update_listener in self.__display_item_needs_update_listeners:
            display_item_needs_update_listener.close()
        self.__display_item_needs_update_listeners = None
        self.__display_items = None
        self.on_selection_changed = None
        self.on_context_menu_event = None
        self.on_drag_started = None
        self.on_focus_changed = None
        self.on_delete_display_items = None
        self.__closed = True

    def __update_display_items(self):
        with self.__changed_display_items_mutex:
            changed_display_items = self.__changed_display_items
            self.__changed_display_items = False
        if changed_display_items:
            self.icon_view_canvas_item.update()

    def set_selected_index(self, index):
        self.selection.set(index)

    def make_selection_visible(self):
        self.icon_view_canvas_item.make_selection_visible()

    # this message comes from the canvas item when delete key is pressed
    def _delete_pressed(self):
        if self.on_delete_display_items:
            self.on_delete_display_items([self.__display_items[index] for index in self.selection.indexes])

    @property
    def display_item_count(self):
        return len(self.__display_items)

    def context_menu_event(self, index, x, y, gx, gy):
        if self.on_context_menu_event:
            display_item = self.__display_items[index] if index is not None else None
            return self.on_context_menu_event(display_item, x, y, gx, gy)
        return False

    def drag_started(self, index, x, y, modifiers):
        mime_data, thumbnail_data = self.__display_items[index].drag_started(self.ui, x, y, modifiers)
        if mime_data:
            if self.on_drag_started:
                self.on_drag_started(mime_data, thumbnail_data)

    def __display_item_needs_update(self):
        with self.__changed_display_items_mutex:
            self.__changed_display_items = True
            self.__add_task(str(id(self)), self.__update_display_items)

    # call this method to insert a display item
    # not thread safe
    def display_item_inserted(self, display_item, before_index):
        self.__display_items.insert(before_index, display_item)
        self.__display_item_needs_update_listeners.insert(before_index, display_item.needs_update_event.listen(self.__display_item_needs_update))
        # tell the icon view to update.
        self.icon_view_canvas_item.update()

    # call this method to remove a display item (by index)
    # not thread safe
    def display_item_removed(self, index):
        self.__display_item_needs_update_listeners[index].close()
        del self.__display_item_needs_update_listeners[index]
        del self.__display_items[index]
        self.icon_view_canvas_item.update()

    def paint_item(self, drawing_context, index, rect, is_selected):
        drawing_context.add(self.__display_items[index].draw_thumbnail(self.dispatch_task, self.ui, rect.inset(6)))


class DataListWidget(Widgets.CompositeWidgetBase):

    def __init__(self, ui, data_list_controller):
        super().__init__(ui.create_column_widget())
        self.data_list_controller = data_list_controller
        data_list_widget = ui.create_canvas_widget()
        data_list_widget.canvas_item.add_canvas_item(data_list_controller.canvas_item)
        self.content_widget.add(data_list_widget)

        def data_list_drag_started(mime_data, thumbnail_data):
            self.drag(mime_data, thumbnail_data)

        data_list_controller.on_drag_started = data_list_drag_started

    def close(self):
        self.data_list_controller.on_drag_started = None
        self.data_list_controller = None
        super().close()


class DataGridWidget(Widgets.CompositeWidgetBase):

    def __init__(self, ui, data_grid_controller):
        super().__init__(ui.create_column_widget())
        self.data_grid_controller = data_grid_controller
        data_grid_widget = ui.create_canvas_widget()
        data_grid_widget.canvas_item.add_canvas_item(data_grid_controller.canvas_item)
        self.content_widget.add(data_grid_widget)

        def data_list_drag_started(mime_data, thumbnail_data):
            self.drag(mime_data, thumbnail_data)

        data_grid_controller.on_drag_started = data_list_drag_started

    def close(self):
        self.data_grid_controller.on_drag_started = None
        self.data_grid_controller = None
        super().close()


class DataBrowserController(object):

    def __init__(self, document_controller, selection):
        self.document_controller = document_controller
        self.__focused = False
        self.__data_group = None
        self.__data_item = None
        self.__filter_id = None
        self.__selected_display_items = list()
        self.selection = selection
        self.filter_changed_event = Event.Event()
        self.selection_changed_event = Event.Event()
        self.selected_data_items_changed_event = Event.Event()
        self.document_controller.set_data_group_or_filter(None, None)  # MARK. consolidate to one object.
        # TODO: Restructure data browser controller to avoid using re-entry flag
        self.__blocked = False  # ugh. the smell of bad design.

    def close(self):
        pass

    @property
    def focused(self):
        return self.__focused

    @focused.setter
    def focused(self, focused):
        if focused:
            self.document_controller.notify_selected_data_item_changed(self.__data_item)
        self.__focused = focused

    def set_data_browser_selection(self, data_group=None, data_item=None, filter_id=None):
        if not self.__blocked:
            self.__blocked = True
            try:
                trigger = False

                if data_item and data_item.category == "temporary":
                    filter_id = "temporary"
                else:
                    pass  # use the one that was passed in

                # check to see if the filter changed
                if data_group != self.__data_group or filter_id != self.__filter_id:
                    # store the selection so we know when it changes
                    self.__data_group = data_group
                    self.__filter_id = filter_id
                    # update the data group that the data item model is tracking. the changes will be queued to the ui thread even
                    # though this is already on the ui thread.
                    self.document_controller.set_data_group_or_filter(data_group, filter_id)  # MARK. consolidate to one object.
                    # when the data group or filter is changed (prior to this handler being called), it will generate a new
                    # list of items to be displayed in the data browser and that new list will be queued in case it is called on
                    # a background thread (it isn't in this case). call periodic to actually sync the changes to the data
                    # browser ui.
                    self.document_controller.periodic()
                    # fire the filter changed event
                    self.filter_changed_event.fire(data_group, filter_id)
                    trigger = True

                # check to see if the data item changed
                if self.__data_item != data_item:
                    self.__data_item = data_item
                    trigger = True

                if trigger:
                    self.selection_changed_event.fire(data_item)
            finally:
                self.__blocked = False

    @property
    def selected_data_items(self):
        return [display_item.data_item for display_item in self.__selected_display_items] if self.__focused else list()

    @property
    def data_item(self):
        return self.__data_item if self.__focused else None

    def selected_display_items_changed(self, display_items):
        # when the selection is changed in the ui, call this method to synchronize.
        # it will trigger the SelectedDataItemBinding to update.

        if len(display_items) == 0:
            data_item = None
            self.set_data_browser_selection(self.__data_group, None, self.__filter_id)
        elif len(display_items) == 1:
            data_item = display_items[0].data_item
            self.set_data_browser_selection(self.__data_group, data_item, self.__filter_id)
        else:
            data_item = None

        if self.__focused:
            self.document_controller.notify_selected_data_item_changed(data_item)

        self.__selected_display_items = copy.copy(display_items)

    def create_display_item_context_menu(self, display_item):
        if display_item is not None:
            return self.document_controller.create_context_menu_for_data_item(display_item.data_item)
        else:
            return self.document_controller.ui.create_context_menu(self.document_controller.document_window)

    def display_item_double_clicked(self, display_item):
        self.document_controller.new_window_with_data_item("data", data_item=display_item.data_item)

    def delete_display_items(self, display_items):
        for display_item in copy.copy(display_items):
            data_item = display_item.data_item
            container = self.document_controller.data_items_binding.container
            container = DataGroup.get_data_item_container(container, data_item)
            if container and data_item in container.data_items:
                container.remove_data_item(data_item)
                # Note: periodic is here because the one in browser display panel is there.
                # I could not get a test to fail without this statement; but regular use
                # seems to fail during delete if this isn't here. Argh. Bad design.
                # TODO: avoid calling periodic by reworking thread support in data panel
                self.document_controller.periodic()  # keep the display items in data panel consistent.


class LibraryModelController(object):
    """Controller for a list of top level library items."""

    def __init__(self, ui, item_controllers):
        """
        item_controllers is a list of objects that have a title property and a on_title_changed callback that gets
        invoked (on the ui thread) when the title changes externally.
        """
        self.ui = ui
        self.item_model_controller = self.ui.create_item_model_controller(["display"])
        self.item_model_controller.on_item_drop_mime_data = lambda mime_data, action, row, parent_row, parent_id: self.item_drop_mime_data(mime_data, action, row, parent_row, parent_id)
        self.item_model_controller.supported_drop_actions = self.item_model_controller.DRAG | self.item_model_controller.DROP
        self.item_model_controller.mime_types_for_drop = ["text/uri-list", "text/data_item_uuid"]
        self.on_receive_files = None
        self.__item_controllers = list()
        self.__item_count = 0
        # build the items
        for item_controller in item_controllers:
            self.__append_item_controller(item_controller)

    def close(self):
        self.__item_controllers = None
        self.item_model_controller.close()
        self.item_model_controller = None
        self.on_receive_files = None

    # not thread safe. must be called on ui thread.
    def __append_item_controller(self, item_controller):
        parent_item = self.item_model_controller.root
        self.item_model_controller.begin_insert(self.__item_count, self.__item_count, parent_item.row, parent_item.id)
        item = self.item_model_controller.create_item()
        parent_item.insert_child(self.__item_count, item)
        self.item_model_controller.end_insert()
        # not thread safe. must be called on ui thread.
        def title_changed(title):
            item.data["display"] = title
            self.item_model_controller.data_changed(item.row, item.parent.row, item.parent.id)
        item_controller.on_title_changed = title_changed
        title_changed(item_controller.title)
        self.__item_controllers.append(item_controller)
        self.__item_count += 1

    def item_drop_mime_data(self, mime_data, action, row, parent_row, parent_id):
        if mime_data.has_file_paths:
            if row >= 0:  # only accept drops ONTO items, not BETWEEN items
                return self.item_model_controller.NONE
            if self.on_receive_files and self.on_receive_files(mime_data.file_paths):
                return self.item_model_controller.COPY
        return self.item_model_controller.NONE


class DataGroupModelController(object):
    """ A tree model of the data groups. this class watches for changes to the data groups contained in the document
    controller and responds by updating the item model controller associated with the data group tree view widget. it
    also handles drag and drop and keeps the current selection synchronized with the image panel. """

    def __init__(self, document_controller):
        self.ui = document_controller.ui
        self.item_model_controller = self.ui.create_item_model_controller(["display", "edit"])
        self.item_model_controller.on_item_set_data = self.item_set_data
        self.item_model_controller.on_item_drop_mime_data = self.item_drop_mime_data
        self.item_model_controller.on_item_mime_data = self.item_mime_data
        self.item_model_controller.on_remove_rows = self.remove_rows
        self.item_model_controller.supported_drop_actions = self.item_model_controller.DRAG | self.item_model_controller.DROP
        self.item_model_controller.mime_types_for_drop = ["text/uri-list", "text/data_item_uuid", "text/data_group_uuid"]
        self.__document_controller_weakref = weakref.ref(document_controller)
        document_model = self.document_controller.document_model
        self.__document_model_item_inserted_listener = document_model.item_inserted_event.listen(functools.partial(self.item_inserted, document_model))
        self.__document_model_item_removed_listener = document_model.item_removed_event.listen(functools.partial(self.item_removed, document_model))
        self.__mapping = { document_controller.document_model: self.item_model_controller.root }
        self.on_receive_files = None
        # add items that already exist
        self.__data_group_property_changed_listeners = dict()
        self.__data_group_item_inserted_listeners = dict()
        self.__data_group_item_removed_listeners = dict()
        self.__data_group_data_item_inserted_listeners = dict()
        self.__data_group_data_item_removed_listeners = dict()
        data_groups = document_controller.document_model.data_groups
        for index, data_group in enumerate(data_groups):
            self.item_inserted(document_controller.document_model, "data_groups", data_group, index)

    def close(self):
        # cheap way to unlisten to everything
        for object in self.__mapping.keys():
            if isinstance(object, DataGroup.DataGroup):
                self.__data_group_data_item_inserted_listeners[object.uuid].close()
                del self.__data_group_data_item_inserted_listeners[object.uuid]
                self.__data_group_data_item_removed_listeners[object.uuid].close()
                del self.__data_group_data_item_removed_listeners[object.uuid]
                self.__data_group_item_inserted_listeners[object.uuid].close()
                del self.__data_group_item_inserted_listeners[object.uuid]
                self.__data_group_item_removed_listeners[object.uuid].close()
                del self.__data_group_item_removed_listeners[object.uuid]
                self.__data_group_property_changed_listeners[object.uuid].close()
                del self.__data_group_property_changed_listeners[object.uuid]
        self.__document_model_item_inserted_listener.close()
        self.__document_model_item_inserted_listener = None
        self.__document_model_item_removed_listener.close()
        self.__document_model_item_removed_listener = None
        self.item_model_controller.close()
        self.item_model_controller = None
        self.on_receive_files = None

    def log(self, parent_id=-1, indent=""):
        parent_id = parent_id if parent_id >= 0 else self.item_model_controller.root.id
        for index, child in enumerate(self.item_model_controller.item_from_id(parent_id).children):
            value = child.data["display"] if "display" in child.data else "---"
            logging.debug(indent + str(index) + ": (" + str(child.id) + ") " + value)
            self.log(child.id, indent + "  ")

    @property
    def document_controller(self):
        return self.__document_controller_weakref()

    # these two methods support the 'count' display for data groups. they count up
    # the data items that are children of the container (which can be a data group
    # or a document controller) and also data items in all of their child groups.
    def __append_data_item_flat(self, container, data_items):
        if isinstance(container, DataItem.DataItem):
            data_items.append(container)
        if hasattr(container, "data_items"):
            for child_data_item in container.data_items:
                self.__append_data_item_flat(child_data_item, data_items)
    def __get_data_item_count_flat(self, container):
        data_items = []
        self.__append_data_item_flat(container, data_items)
        return len(data_items)

    # this message is received when a data item is inserted into one of the
    # groups we're observing.
    def item_inserted(self, container, key, object, before_index):
        if key == "data_groups":
            # manage the item model
            parent_item = self.__mapping[container]
            self.item_model_controller.begin_insert(before_index, before_index, parent_item.row, parent_item.id)
            count = self.__get_data_item_count_flat(object)
            properties = {
                "display": str(object) + (" (%i)" % count),
                "edit": object.title,
                "data_group": object
            }
            item = self.item_model_controller.create_item(properties)
            parent_item.insert_child(before_index, item)
            self.__mapping[object] = item
            def property_changed(key, value):
                if key == "title":
                    self.__update_item_count(object)
            self.__data_group_property_changed_listeners[object.uuid] = object.property_changed_event.listen(property_changed)
            self.__data_group_item_inserted_listeners[object.uuid] = object.item_inserted_event.listen(functools.partial(self.item_inserted, object))
            self.__data_group_item_removed_listeners[object.uuid] = object.item_removed_event.listen(functools.partial(self.item_removed, object))
            self.__data_group_data_item_inserted_listeners[object.uuid] = object.data_item_inserted_event.listen(self.data_item_inserted)
            self.__data_group_data_item_removed_listeners[object.uuid] = object.data_item_removed_event.listen(self.data_item_removed)
            self.item_model_controller.end_insert()
            # recursively insert items that already exist
            data_groups = object.data_groups
            for index, child_data_group in enumerate(data_groups):
                self.item_inserted(object, "data_groups", child_data_group, index)

    # this message is received when a data item is removed from one of the
    # groups we're observing.
    def item_removed(self, container, key, object, index):
        if key == "data_groups":
            assert isinstance(object, DataGroup.DataGroup)
            # get parent and item
            parent_item = self.__mapping[container]
            # manage the item model
            self.item_model_controller.begin_remove(index, index, parent_item.row, parent_item.id)
            self.__data_group_data_item_inserted_listeners[object.uuid].close()
            del self.__data_group_data_item_inserted_listeners[object.uuid]
            self.__data_group_data_item_removed_listeners[object.uuid].close()
            del self.__data_group_data_item_removed_listeners[object.uuid]
            self.__data_group_item_inserted_listeners[object.uuid].close()
            del self.__data_group_item_inserted_listeners[object.uuid]
            self.__data_group_item_removed_listeners[object.uuid].close()
            del self.__data_group_item_removed_listeners[object.uuid]
            self.__data_group_property_changed_listeners[object.uuid].close()
            del self.__data_group_property_changed_listeners[object.uuid]
            parent_item.remove_child(parent_item.children[index])
            self.__mapping.pop(object)
            self.item_model_controller.end_remove()

    def __update_item_count(self, data_group):
        assert isinstance(data_group, DataGroup.DataGroup)
        count = self.__get_data_item_count_flat(data_group)
        item = self.__mapping[data_group]
        item.data["display"] = str(data_group) + (" (%i)" % count)
        item.data["edit"] = data_group.title
        self.item_model_controller.data_changed(item.row, item.parent.row, item.parent.id)

    # this method if called when one of our listened to data groups changes
    def data_item_inserted(self, container, data_item, before_index, moving):
        self.__update_item_count(container)

    # this method if called when one of our listened to data groups changes
    def data_item_removed(self, container, data_item, index, moving):
        self.__update_item_count(container)

    def item_set_data(self, data, index, parent_row, parent_id):
        data_group = self.item_model_controller.item_value("data_group", index, parent_id)
        if data_group:
            data_group.title = data
            return True
        return False

    def get_data_group(self, index, parent_row, parent_id):
        return self.item_model_controller.item_value("data_group", index, parent_id)

    def get_data_group_of_parent(self, parent_row, parent_id):
        parent_item = self.item_model_controller.item_from_id(parent_id)
        return parent_item.data["data_group"] if "data_group" in parent_item.data else None

    def get_data_group_index(self, data_group):
        item = None
        data_group_item = self.__mapping.get(data_group)
        parent_item = data_group_item.parent if data_group_item else self.item_model_controller.root
        assert parent_item is not None
        for child in parent_item.children:
            child_data_group = child.data.get("data_group")
            if child_data_group == data_group:
                item = child
                break
        if item:
            return item.row, item.parent.row, item.parent.id
        else:
            return -1, -1, 0

    def item_drop_mime_data(self, mime_data, action, row, parent_row, parent_id):
        data_group = self.get_data_group_of_parent(parent_row, parent_id)
        container = self.document_controller.document_model if parent_row < 0 and parent_id == 0 else data_group
        if data_group and mime_data.has_file_paths:
            if row >= 0:  # only accept drops ONTO items, not BETWEEN items
                return self.item_model_controller.NONE
            if self.on_receive_files and self.on_receive_files(mime_data.file_paths, data_group, len(data_group.data_items)):
                return self.item_model_controller.COPY
        if data_group and mime_data.has_format("text/data_item_uuid"):
            if row >= 0:  # only accept drops ONTO items, not BETWEEN items
                return self.item_model_controller.NONE
            # if the data item exists in this document, then it is copied to the
            # target group. if it doesn't exist in this document, then it is coming
            # from another document and can't be handled here.
            data_item_uuid = uuid.UUID(mime_data.data_as_string("text/data_item_uuid"))
            data_item = self.document_controller.document_model.get_data_item_by_key(data_item_uuid)
            if data_item:
                data_group.append_data_item(data_item)
                return action
            return self.item_model_controller.NONE
        if mime_data.has_format("text/data_group_uuid"):
            data_group_uuid = uuid.UUID(mime_data.data_as_string("text/data_group_uuid"))
            data_group = self.document_controller.document_model.get_data_group_by_uuid(data_group_uuid)
            if data_group:
                data_group_copy = copy.deepcopy(data_group)
                if row >= 0:
                    container.insert_data_group(row, data_group_copy)
                else:
                    container.append_data_group(data_group_copy)
                return action
        return self.item_model_controller.NONE

    def item_mime_data(self, index, parent_row, parent_id):
        data_group = self.get_data_group(index, parent_row, parent_id)
        if data_group:
            mime_data = self.ui.create_mime_data()
            mime_data.set_data_as_string("text/data_group_uuid", str(data_group.uuid))
            return mime_data
        return None

    def remove_rows(self, row, count, parent_row, parent_id):
        data_group = self.get_data_group_of_parent(parent_row, parent_id)
        container = self.document_controller.document_model if parent_row < 0 and parent_id == 0 else data_group
        for i in range(count):
            del container.data_groups[row]
        return True


class DataPanel(Panel.Panel):

    def __init__(self, document_controller, panel_id, properties):
        super(DataPanel, self).__init__(document_controller, panel_id, _("Data Items"))

        dispatch_task = document_controller.document_model.dispatch_task
        ui = document_controller.ui

        self.__data_browser_controller = document_controller.data_browser_controller
        self.__filter_changed_event_listener = self.__data_browser_controller.filter_changed_event.listen(self.__data_panel_filter_changed)
        self.__selection_changed_event_listener = self.__data_browser_controller.selection_changed_event.listen(self.__data_panel_selection_changed)

        class LibraryItemController(object):

            def __init__(self, base_title, binding):
                self.__base_title = base_title
                self.__count = 0
                self.__binding = binding
                self.on_title_changed = None

                # not thread safe. must be called on ui thread.
                def data_item_inserted(data_item, before_index):
                    self.__count += 1
                    if self.on_title_changed:
                        document_controller.queue_task(functools.partial(self.on_title_changed, self.title))

                # not thread safe. must be called on ui thread.
                def data_item_removed(data_item, index):
                    self.__count -= 1
                    if self.on_title_changed:
                        document_controller.queue_task(functools.partial(self.on_title_changed, self.title))

                self.__binding.inserters[id(self)] = data_item_inserted
                self.__binding.removers[id(self)] = data_item_removed
                self.__count = len(self.__binding.data_items)

            @property
            def title(self):
                return self.__base_title + (" (%i)" % self.__count)

            def close(self):
                del self.__binding.inserters[id(self)]
                del self.__binding.removers[id(self)]
                self.__binding.close()

        all_items_binding = document_controller.create_data_item_binding(None, None)
        all_items_controller = LibraryItemController(_("All"), all_items_binding)
        live_items_binding = document_controller.create_data_item_binding(None, "temporary")
        live_items_controller = LibraryItemController(_("Live"), live_items_binding)
        latest_items_binding = document_controller.create_data_item_binding(None, "latest-session")
        latest_items_controller = LibraryItemController(_("Latest Session"), latest_items_binding)
        self.__item_controllers = [all_items_controller, live_items_controller, latest_items_controller]

        self.library_model_controller = LibraryModelController(ui, self.__item_controllers)
        self.library_model_controller.on_receive_files = self.library_model_receive_files

        self.data_group_model_controller = DataGroupModelController(document_controller)
        self.data_group_model_controller.on_receive_files = lambda file_paths, data_group, index: self.data_group_model_receive_files(file_paths, data_group, index)

        self.__blocked = False

        def library_widget_selection_changed(selected_indexes):
            if not self.__blocked:
                self.__blocked = True
                try:
                    index = selected_indexes[0][0] if len(selected_indexes) > 0 else -1
                    if index == 2:
                        self.__data_browser_controller.set_data_browser_selection(filter_id="latest-session")
                    elif index == 1:
                        self.__data_browser_controller.set_data_browser_selection(filter_id="temporary")
                    else:
                        self.__data_browser_controller.set_data_browser_selection()
                finally:
                    self.__blocked = False

        self.library_widget = ui.create_tree_widget(properties={"height": 24 + 18 * 2})
        self.library_widget.item_model_controller = self.library_model_controller.item_model_controller
        self.library_widget.on_selection_changed = library_widget_selection_changed
        self.library_widget.on_focus_changed = lambda focused: setattr(self, "focused", focused)

        def data_group_widget_selection_changed(selected_indexes):
            if not self.__blocked:
                self.__blocked = True
                try:
                    if len(selected_indexes) > 0:
                        index, parent_row, parent_id = selected_indexes[0]
                        data_group = self.data_group_model_controller.get_data_group(index, parent_row, parent_id)
                    else:
                        data_group = None
                    self.__data_browser_controller.set_data_browser_selection(data_group=data_group)
                finally:
                    self.__blocked = False

        def data_group_widget_key_pressed(index, parent_row, parent_id, key):
            if key.is_delete:
                data_group = self.data_group_model_controller.get_data_group(index, parent_row, parent_id)
                if data_group:
                    container = self.data_group_model_controller.get_data_group_of_parent(parent_row, parent_id)
                    container = container if container else self.document_controller.document_model
                    self.document_controller.remove_data_group_from_container(data_group, container)
            return False

        self.data_group_widget = ui.create_tree_widget()
        self.data_group_widget.item_model_controller = self.data_group_model_controller.item_model_controller
        self.data_group_widget.on_selection_changed = data_group_widget_selection_changed
        self.data_group_widget.on_item_key_pressed = data_group_widget_key_pressed
        self.data_group_widget.on_focus_changed = lambda focused: setattr(self, "focused", focused)

        library_label_row = ui.create_row_widget()
        library_label = ui.create_label_widget(_("Library"), properties={"stylesheet": "font-weight: bold"})
        library_label_row.add_spacing(8)
        library_label_row.add(library_label)
        library_label_row.add_stretch()

        collections_label_row = ui.create_row_widget()
        collections_label = ui.create_label_widget(_("Collections"), properties={"stylesheet": "font-weight: bold"})
        collections_label_row.add_spacing(8)
        collections_label_row.add(collections_label)
        collections_label_row.add_stretch()

        library_section_widget = ui.create_column_widget()
        library_section_widget.add_spacing(4)
        library_section_widget.add(library_label_row)
        library_section_widget.add(self.library_widget)

        collections_section_widget = ui.create_column_widget()
        collections_section_widget.add_spacing(4)
        collections_section_widget.add(collections_label_row)
        collections_section_widget.add(self.data_group_widget)

        master_widget = ui.create_column_widget()
        master_widget.add(library_section_widget)
        master_widget.add(collections_section_widget)
        master_widget.add_stretch()

        def show_context_menu(display_item, x, y, gx, gy):
            menu = self.__data_browser_controller.create_display_item_context_menu(display_item)
            menu.popup(gx, gy)
            return True

        selection = self.__data_browser_controller.selection
        self.data_list_controller = DataListController(dispatch_task, document_controller.add_task, document_controller.clear_task, ui, selection)
        self.data_list_controller.on_selection_changed = self.__data_browser_controller.selected_display_items_changed
        self.data_list_controller.on_context_menu_event = show_context_menu
        self.data_list_controller.on_display_item_double_clicked = self.__data_browser_controller.display_item_double_clicked
        self.data_list_controller.on_focus_changed = lambda focused: setattr(self.__data_browser_controller, "focused", focused)
        self.data_list_controller.on_delete_display_items = self.__data_browser_controller.delete_display_items

        self.data_grid_controller = DataGridController(dispatch_task, document_controller.add_task, document_controller.clear_task, ui, selection)
        self.data_grid_controller.on_selection_changed = self.__data_browser_controller.selected_display_items_changed
        self.data_grid_controller.on_context_menu_event = show_context_menu
        self.data_grid_controller.on_display_item_double_clicked = self.__data_browser_controller.display_item_double_clicked
        self.data_grid_controller.on_focus_changed = lambda focused: setattr(self.__data_browser_controller, "focused", focused)
        self.data_grid_controller.on_delete_display_items = self.__data_browser_controller.delete_display_items

        data_list_widget = DataListWidget(ui, self.data_list_controller)
        data_grid_widget = DataGridWidget(ui, self.data_grid_controller)

        def data_item_inserted(data_item, before_index):
            display_item = DisplayItem(data_item, dispatch_task, ui)
            self.__display_items.insert(before_index, display_item)
            self.data_list_controller.display_item_inserted(display_item, before_index)
            self.data_grid_controller.display_item_inserted(display_item, before_index)

        def data_item_removed(index):
            self.data_list_controller.display_item_removed(index)
            self.data_grid_controller.display_item_removed(index)
            self.__display_items[index].close()
            del self.__display_items[index]

        self.__binding = document_controller.filtered_data_items_binding
        self.__binding.inserters[id(self)] = lambda data_item, before_index: self.document_controller.queue_task(functools.partial(data_item_inserted, data_item, before_index))
        self.__binding.removers[id(self)] = lambda data_item, index: self.document_controller.queue_task(functools.partial(data_item_removed, index))

        self.__display_items = list()

        for index, data_item in enumerate(self.__binding.data_items):
            data_item_inserted(data_item, index)

        list_icon_button = CanvasItem.BitmapButtonCanvasItem(ui.load_rgba_data_from_file(Decorators.relative_file(__file__, "resources/list_icon_20.png")))
        grid_icon_button = CanvasItem.BitmapButtonCanvasItem(ui.load_rgba_data_from_file(Decorators.relative_file(__file__, "resources/grid_icon_20.png")))

        list_icon_button.sizing.set_fixed_size(Geometry.IntSize(20, 20))
        grid_icon_button.sizing.set_fixed_size(Geometry.IntSize(20, 20))

        button_row = CanvasItem.CanvasItemComposition()
        button_row.layout = CanvasItem.CanvasItemRowLayout(spacing=4)
        button_row.add_canvas_item(list_icon_button)
        button_row.add_canvas_item(grid_icon_button)

        buttons_widget = ui.create_canvas_widget(properties={"height": 20, "width": 44})
        buttons_widget.canvas_item.add_canvas_item(button_row)

        search_widget = ui.create_row_widget()
        search_widget.add_spacing(8)
        search_widget.add(ui.create_label_widget(_("Filter")))
        search_widget.add_spacing(8)
        search_line_edit = ui.create_line_edit_widget()
        search_line_edit.placeholder_text = _("No Filter")
        # search_line_edit.clear_button_enabled = True  # Qt 5.3 doesn't signal text edited or editing finished when clearing. useless so disabled.
        search_line_edit.on_text_edited = self.document_controller.filter_controller.text_filter_changed
        search_line_edit.on_editing_finished = self.document_controller.filter_controller.text_filter_changed
        search_widget.add(search_line_edit)
        search_widget.add_spacing(6)
        search_widget.add(buttons_widget)
        search_widget.add_spacing(8)

        self.data_view_widget = ui.create_stack_widget()
        self.data_view_widget.add(data_list_widget)
        self.data_view_widget.add(data_grid_widget)
        self.data_view_widget.current_index = 0

        self.__view_button_group = CanvasItem.RadioButtonGroup([list_icon_button, grid_icon_button])
        self.__view_button_group.current_index = 0
        self.__view_button_group.on_current_index_changed = lambda index: setattr(self.data_view_widget, "current_index", index)

        slave_widget = ui.create_column_widget()
        slave_widget.add(self.data_view_widget)
        slave_widget.add_spacing(6)
        slave_widget.add(search_widget)
        slave_widget.add_spacing(6)

        self.splitter = ui.create_splitter_widget("vertical", properties)
        self.splitter.orientation = "vertical"
        self.splitter.add(master_widget)
        self.splitter.add(slave_widget)
        self.splitter.restore_state("window/v1/data_panel_splitter")

        self.widget = self.splitter

        self._data_list_widget = data_list_widget
        self._data_grid_widget = data_grid_widget

    def close(self):
        # binding should not be closed since it isn't created in this object
        self.splitter.save_state("window/v1/data_panel_splitter")
        self.__data_browser_controller.set_data_browser_selection()
        del self.__binding.inserters[id(self)]
        del self.__binding.removers[id(self)]
        # close the models
        self.data_group_model_controller.close()
        self.data_group_model_controller = None
        for item_controller in self.__item_controllers:
            item_controller.close()
        self.library_model_controller.close()
        self.library_model_controller = None
        self.data_list_controller.close()
        self.data_list_controller = None
        self.data_grid_controller.close()
        self.data_grid_controller = None
        # display items
        for display_item in self.__display_items:
            display_item.close()
        self.__display_items = None
        self.__filter_changed_event_listener.close()
        self.__filter_changed_event_listener = None
        self.__selection_changed_event_listener.close()
        self.__selection_changed_event_listener = None
        # button group
        self.__view_button_group.close()
        self.__view_button_group = None
        # finish closing
        super(DataPanel, self).close()

    # the focused property gets set from on_focus_changed on the data item widget. when gaining focus,
    # make sure the document controller knows what is selected so it can update the inspector.
    @property
    def focused(self):
        return self.__data_browser_controller.focused

    @focused.setter
    def focused(self, focused):
        self.__data_browser_controller.focused = focused

    def __data_panel_filter_changed(self, data_group, filter_id):
        if data_group:
            index, parent_row, parent_id = self.data_group_model_controller.get_data_group_index(data_group)
            self.library_widget.clear_current_row()
            self.data_group_widget.set_current_row(index, parent_row, parent_id)
        else:
            self.data_group_widget.clear_current_row()
            if filter_id == "latest-session":
                self.library_widget.set_current_row(2, -1, 0)  # select the 'latest' group
            elif filter_id == "temporary":
                self.library_widget.set_current_row(1, -1, 0)  # select the 'live' group
            else:
                self.library_widget.set_current_row(0, -1, 0)  # select the 'all' group

    def __data_panel_selection_changed(self, data_item):
        # update the data item selection by determining the new index of the item, if any.
        data_item_index = -1
        for index in range(len(self.__display_items)):
            if data_item == self.__display_items[index].data_item:
                data_item_index = index
                break
        if self.data_view_widget.current_index == 0:
            if data_item_index >= 0:
                self.data_list_controller.set_selected_index(data_item_index)
                self.data_list_controller.make_selection_visible()
            else:
                self.data_list_controller.selection.clear()
        else:
            if data_item_index >= 0:
                self.data_grid_controller.set_selected_index(data_item_index)
                self.data_grid_controller.make_selection_visible()
            else:
                self.data_grid_controller.selection.clear()

    def library_model_receive_files(self, file_paths, threaded=True):
        def receive_files_complete(received_data_items):
            def select_library_all():
                self.__data_browser_controller.set_data_browser_selection(data_item=received_data_items[0])
            if len(received_data_items) > 0:
                self.queue_task(select_library_all)

        document_model = self.document_controller.document_model
        index = len(document_model.data_items)
        self.document_controller.receive_files(document_model, file_paths, None, index, threaded, receive_files_complete)
        return True

    # receive files dropped into the data group.
    # this message comes from the data group model, which is why it is named the way it is.
    def data_group_model_receive_files(self, file_paths, data_group, index, threaded=True):
        def receive_files_complete(received_data_items):
            def select_data_group_and_data_item():
                self.__data_browser_controller.set_data_browser_selection(data_group=data_group, data_item=received_data_items[0])
            if len(received_data_items) > 0:
                if threaded:
                    self.queue_task(select_data_group_and_data_item)
                else:
                    select_data_group_and_data_item()

        document_model = self.document_controller.document_model
        self.document_controller.receive_files(document_model, file_paths, data_group, index, threaded, receive_files_complete)
        return True
