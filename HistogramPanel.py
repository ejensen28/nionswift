# standard libraries
import gettext
import logging
import threading
import time

# third party libraries
import numpy

# local libraries
from nion.swift.Decorators import ProcessingThread
from nion.swift.Decorators import relative_file
from nion.swift.Decorators import queue_main_thread_sync
from nion.swift import Panel
from nion.swift import UserInterface

_ = gettext.gettext


class HistogramThread(ProcessingThread):

    def __init__(self, histogram_panel):
        super(HistogramThread, self).__init__(minimum_interval=0.2)
        self.__histogram_panel = histogram_panel
        self.__data_item = None
        self.__mutex = threading.RLock()  # access to the data item
        # mutex is needed to avoid case where grab data is called
        # simultaneously to handle_data and data item would get
        # released twice, once in handle data and once in the final
        # call to release data.
        # don't start until everything is initialized
        self.start()

    def close(self):
        super(HistogramThread, self).close()
        # protect against handle_data being called, but the data
        # was never grabbed. this must go _after_ the super.close
        with self.__mutex:
            if self.__data_item:
                self.__data_item.remove_ref()

    def handle_data(self, data_item):
        with self.__mutex:
            if self.__data_item:
                self.__data_item.remove_ref()
            self.__data_item = data_item
        if data_item:
            data_item.add_ref()

    def grab_data(self):
        with self.__mutex:
            data_item = self.__data_item
            self.__data_item = None
            return data_item

    def process_data(self, data_item):
        self.__histogram_panel._set_data_item(data_item)

    def release_data(self, data_item):
        data_item.remove_ref()


class HistogramPanel(Panel.Panel):

    delay_queue = property(lambda self: self.document_controller)

    def __init__(self, document_controller, panel_id, properties):
        super(HistogramPanel, self).__init__(document_controller, panel_id, _("Histogram"))

        ui = document_controller.ui

        self.canvas = ui.create_canvas_widget(properties)

        self.canvas.on_size_changed = lambda width, height: self.size_changed(width, height)
        self.canvas.on_mouse_double_clicked = lambda x, y, modifiers: self.mouse_double_clicked(x, y, modifiers)
        self.canvas.on_mouse_pressed = lambda x, y, modifiers: self.mouse_pressed(x, y, modifiers)
        self.canvas.on_mouse_released = lambda x, y, modifiers: self.mouse_released(x, y, modifiers)
        self.canvas.on_mouse_position_changed = lambda x, y, modifiers: self.mouse_position_changed(x, y, modifiers)

        self.widget = self.canvas

        # connect self as listener. this will result in calls to selected_data_item_changed
        self.document_controller.add_listener(self)

        self.__data_item = None

        # these are the drawn display limits. useful during tracking or when viewing display
        # limits within the context of the broader range of data.
        self.__display_limits = (0,1)

        self.pressed = False

        self.__histogram_layer = self.canvas.create_layer()
        self.__adornments_layer = self.canvas.create_layer()

        self.__histogram_dirty = True
        self.__adornments_dirty = True

        self.__update_lock = threading.Lock()

        self.__histogram_thread = HistogramThread(self)

    def close(self):
        self.__histogram_thread.close()
        self.__histogram_thread = None
        # first set the data item to None
        self.selected_data_item_changed(None, {"property": "source"})
        # disconnect self as listener
        self.document_controller.remove_listener(self)
        # finish closing
        super(HistogramPanel, self).close()

    def __set_display_limits(self, display_limits):
        self.__display_limits = display_limits
        self.__adornments_dirty = True
        self.__update_histogram()

    def size_changed(self, width, height):
        if width > 0 and height > 0:
            self.__histogram_dirty = True
            self.__update_histogram()

    def mouse_double_clicked(self, x, y, modifiers):
        self.__set_display_limits((0, 1))
        if self.__data_item:
            self.__data_item.display_limits = None

    def mouse_pressed(self, x, y, modifiers):
        self.pressed = True
        self.start = float(x)/self.canvas.width
        self.__set_display_limits((self.start, self.start))

    def mouse_released(self, x, y, modifiers):
        self.pressed = False
        display_limit_range = self.__display_limits[1] - self.__display_limits[0]
        if self.__data_item and (display_limit_range > 0) and (display_limit_range < 1):
            data_min, data_max = self.__data_item.display_range
            lower_display_limit = data_min + self.__display_limits[0] * (data_max - data_min)
            upper_display_limit = data_min + self.__display_limits[1] * (data_max - data_min)
            self.__data_item.display_limits = (lower_display_limit, upper_display_limit)

    def mouse_position_changed(self, x, y, modifiers):
        canvas_width = self.canvas.width
        canvas_height = self.canvas.height
        if self.pressed:
            current = float(x)/canvas_width
            self.__set_display_limits((min(self.start, current), max(self.start, current)))

    # make the histogram from the data item.
    # at the end of this method, both histogram_data and histogram_js will be valid, although data may be None.
    # histogram_js will never be None after this method is called as long as the widget is valid.
    def __make_histogram(self):

        histogram_data = self.__data_item.get_histogram_data() if self.__data_item else None

        if self.__histogram_dirty and (histogram_data is not None and len(histogram_data) > 0):

            self.__histogram_dirty = False

            canvas_width = self.canvas.width
            canvas_height = self.canvas.height

            ctx = self.__histogram_layer.drawing_context

            ctx.clear()
            
            # draw the histogram itself
            ctx.save()
            ctx.begin_path()
            ctx.move_to(0, canvas_height)
            ctx.line_to(0, canvas_height * (1 - histogram_data[0]))
            for i in xrange(1,canvas_width,2):
                ctx.line_to(i, canvas_height * (1 - histogram_data[int(len(histogram_data)*float(i)/canvas_width)]))
            ctx.line_to(canvas_width, canvas_height)
            ctx.close_path()
            ctx.fill_style = "#888"
            ctx.fill()
            ctx.line_width = 1
            ctx.stroke_style = "#00F"
            ctx.stroke()
            ctx.restore()

    def __make_adornments(self):

        if self.widget and self.__adornments_dirty:

            self.__adornments_dirty = False

            canvas_width = self.canvas.width
            canvas_height = self.canvas.height

            ctx = self.__adornments_layer.drawing_context

            ctx.clear()

            left = self.__display_limits[0]
            right = self.__display_limits[1]

            # draw left display limit
            ctx.save()
            ctx.begin_path()
            ctx.move_to(left * canvas_width, 0)
            ctx.line_to(left * canvas_width, canvas_height)
            ctx.close_path()
            ctx.line_width = 2
            ctx.stroke_style = "#000"
            ctx.stroke()
            ctx.restore()

            # draw right display limit
            ctx.save()
            ctx.begin_path()
            ctx.move_to(right * canvas_width, 0)
            ctx.line_to(right * canvas_width, canvas_height)
            ctx.close_path()
            ctx.line_width = 2
            ctx.stroke_style = "#FFF"
            ctx.stroke()
            ctx.restore()

            # draw border
            ctx.save()
            ctx.begin_path()
            ctx.move_to(0,0)
            ctx.line_to(canvas_width,0)
            ctx.line_to(canvas_width,canvas_height)
            ctx.line_to(0,canvas_height)
            ctx.close_path()
            ctx.line_width = 1
            ctx.stroke_style = "#000"
            ctx.stroke()
            ctx.restore()

    # used for queue_main_thread decorator
    delay_queue = property(lambda self: self.document_controller)

    def __update_canvas(self):
        if self.ui and self.widget:
            self.canvas.draw()

    def __update_histogram(self):
        if self.ui and self.widget:
            with self.__update_lock:
                self.__make_histogram()
                self.__make_adornments()
                self.__update_canvas()

    # _get_data_item is only used for testing
    def _get_data_item(self):
        return self.__data_item
    def _set_data_item(self, data_item):
        # this will get invoked whenever the data item changes too. it gets invoked
        # from the histogram thread which gets triggered via the selected_data_item_changed
        # message below.
        self.__data_item = data_item
        # if the user is currently dragging the display limits, we don't want to update
        # from changing data at the same time. but we _do_ want to draw the updated data.
        if not self.pressed:
            self.__display_limits = (0, 1)
        self.__histogram_dirty = True
        self.__adornments_dirty = True
        self.__update_histogram()

    # this message is received from the document controller.
    # it is established using add_listener
    def selected_data_item_changed(self, data_item, info):
        if self.__histogram_thread:
            self.__histogram_thread.update_data(data_item)
