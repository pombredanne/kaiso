from kaiso import types


class TemporaryStaticTypes(object):
    """ Testing helper to define temporary static types

    Types defined inside this context only live for the duration of a test,
    and so won't have have side effects leaking into other tests

    Usage:
        with TemporaryStaticTypes():
            class Foo(Entity):
                pass

    or as a pytest fixture

        @pytest.fixture
        def foo(request):
            patcher = TemporaryStaticTypes()
            patcher.start()
            request.addfinalizer(patcher.stop)
            class Foo(Entity):
                pass
            return Foo
    """

    def start(self):
        self.original = types.collected_static_classes.keys()

    def stop(self):
        added = set(types.collected_static_classes) - set(self.original)
        for type_id in added:
            types.collected_static_classes.pop(type_id)

    def __enter__(self):
        self.start()

    def __exit__(self, *args, **kwargs):
        self.stop()
