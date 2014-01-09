from kaiso import types


class TemporaryStaticTypes(object):
    """ Testing helper to define temporary static types

    Types defined inside this context only live for the duration of a test,
    and so won't have have side effects leaking into other tests

    Usage:
        with TemporaryStaticTypes():
            class Foo(Entity):
                pass

    Though generally via the funcarg

        @pytest.fixture
        def foo(temporary_static_types):
            class Foo(Entity):
                pass
            return Foo

    or
        def test_foo(temporary_static_types):
            class Foo(Entity):
                pass

            assert(...)
    """

    def start(self):
        self.original = types.collected_static_classes.get_type_ids()

    def stop(self):
        current_type_ids = types.collected_static_classes.get_type_ids()
        added = set(current_type_ids) - set(self.original)
        for type_id in added:
            types.collected_static_classes.remove(type_id)

    def __enter__(self):
        self.start()

    def __exit__(self, *args, **kwargs):
        self.stop()
