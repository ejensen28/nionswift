import threading
import time
import weakref


class DataItemProcessor(object):

    def __init__(self, item, cache_property_name):
        self.__weak_item = weakref.ref(item)
        self.__cache_property_name = cache_property_name
        self.__mutex = threading.RLock()
        self.__in_progress = False
        self.__finish_event = threading.Event()
        self.__finish_event.set()
        self.__cached_value = None
        self.__cached_value_dirty = None

    def close(self):
        self.__finish_event.wait()

    def __get_item(self):
        return self.__weak_item() if self.__weak_item else None
    item = property(__get_item)

    def data_item_changed(self):
        """ Called directly from data item. """
        self.set_cached_value_dirty()

    def item_property_changed(self, key, value):
        """
            Called directly from data item.
            Subclasses should override and call set_cached_value_dirty to add
            property dependencies.
        """
        pass

    def set_cached_value_dirty(self):
        self.item.set_cached_value_dirty(self.__cache_property_name)
        self.__cached_value_dirty = True

    def get_calculated_data(self, ui, data):
        """ Subclasses must implement. """
        raise NotImplementedError()

    def get_default_data(self):
        return None

    def get_data_item(self):
        """ Subclasses must implement. """
        raise NotImplementedError()

    def get_data(self, ui, completion_fn=None):
        if (self.__cached_value_dirty is not None and self.__cached_value_dirty) or self.item.is_cached_value_dirty(self.__cache_property_name):
            item = self.item  # hold it
            data_item = self.get_data_item()
            if not data_item.closed:
                data_outputs = data_item.data_outputs
                if len(data_outputs) == 1:
                    def load_data_on_thread(item_hold):
                        time.sleep(0.5)
                        data = data_outputs[0].data
                        if data is not None:  # for data to load and make sure it has data
                            try:
                                calculated_data = self.get_calculated_data(ui, data)
                            except Exception as e:
                                import traceback
                                traceback.print_exc()
                                self.__finish_event.set()
                                raise
                            self.item.set_cached_value(self.__cache_property_name, calculated_data)
                            self.__cached_value = calculated_data
                            self.__cached_value_dirty = False
                        else:
                            calculated_data = None
                        if calculated_data is None:
                            calculated_data = self.get_default_data()
                            self.item.remove_cached_value(self.__cache_property_name)
                            self.__cached_value = None
                            self.__cached_value_dirty = None
                        if completion_fn:
                            completion_fn(calculated_data)
                        with self.__mutex:
                            self.__in_progress = False
                        self.__finish_event.set()
                    with self.__mutex:
                        if not self.__in_progress:
                            self.__in_progress = True
                            self.__finish_event.clear()
                            self.item.add_shared_task(self.__cache_property_name, None, lambda: load_data_on_thread(item))
        calculated_data = self.__cached_value  # self.item.get_cached_value(self.__cache_property_name)
        if calculated_data is not None:
            return calculated_data
        return self.get_default_data()
