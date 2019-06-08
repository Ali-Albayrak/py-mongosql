from enum import Enum

from ..query import MongoQuery
from ..util.history_proxy import ModelHistoryProxy
from .crudhelper import CrudHelper, StrictCrudHelper

from typing import Iterable, Mapping, Set, Union, Tuple, Callable
from sqlalchemy.orm import Query, Session


class CRUD_METHOD(Enum):
    """ CRUD method """
    GET = 'GET'
    LIST = 'LIST'
    CREATE = 'CREATE'
    UPDATE = 'UPDATE'
    DELETE = 'DELETE'


class CrudViewMixin:
    """ Base class for implementations of CRUD views. This class is supposed to be re-initialized for every request.

        To implement a CRUD view:
        1. Implement some method to extract the Query Object from the request
        2. Set `crudhelper` at the class level, initialize it with the proper settings
        3. Implement the _get_db_session() method
        4. If necessary, implement the _save_hook() to customize new & updated entities
        5. Override _method_list() and _method_get() to customize its output
        6. Override _method_create(), _method_update(), _method_delete() and implement saving to the DB

        For an example on how to use CrudViewMixin, see this implementation:

            tests/crud_view.py

        Attrs:
            _mongoquery (MongoQuery):
                The MongoQuery object used to process this query.
    """

    #: Set the CRUD helper object at the class level
    crudhelper = None  # type: Union[CrudHelper, StrictCrudHelper]

    #: List of columns and relationships that must be loaded with MongoQuery.ensure_loaded()
    #: Note that you can also use related columns: "relation.col_name" to ensure it's loaded (join-project)
    #: Remember that every time you use ensure_loaded() on a relationship, you disable filtering for it!
    ensure_loaded = ()

    #: The names of relationships that this View is capable of saving. They will be given to _save_relations() as kwargs
    saves_relations = ()

    def __init__(self):
        #: The MongoQuery for this request, if it was indeed initialized by _mquery()
        self.__mongoquery = None  # type: MongoQuery

        #: The current CRUD method
        self._current_crud_method = None

    def _get_db_session(self) -> Session:
        """ Get a DB session to be used for queries made in this view

        :return: sqlalchemy.orm.Session
        """
        raise NotImplementedError('_get_db_session() not implemented on {}'
                                  .format(type(self)))

    def _get_query_object(self) -> Mapping:
        """ Get the Query Object for the current query.

            Note that the Query Object is not only supported for get() and list() methods, but also for
            create(), update(), and delete(). This enables the API use to request a relationship right away.
        """
        raise NotImplementedError

    # region Hooks

    def _mongoquery_hook(self, mongoquery: MongoQuery) -> MongoQuery:
        """ A hook invoked in _mquery() to modify MongoQuery, if necessary

            This is the last chance to modify a MongoQuery.
            Right after this hook, it end()s, and generates an sqlalchemy Query.

            Use self._current_crud_method to tell what is going on: create, read, update, delete?
        """
        return mongoquery

    def _save_relations(self, _new: object, _prev: Union[object, None] = None, **relations: dict):
        """ A hook that implements saving related models.

        Whenever a relationship is named in the 'saves_relations' class attribute,
        they are plucked out of the incoming JSON dict, and after an entity is created,
        it is passed to this hook.

        Saving a relationship is always a custom procedure; that's why it is implemented through this method.

        In addition to saving relationships, this method can be used to save any custom properties:
        they're plucked out, and handled manually anyway.

        NOTE: this method is executed before _save_hook() is.

        :param _new: The new instance
        :param _prev: Previously persisted version (is provided only when updating).
        :param relations: Values for every relation
        """
        raise NotImplementedError('Saving relations is not yet implemented for this view')

    def _save_hook(self, new: object, prev: Union[object, None] = None):
        """ Hook into create(), update() methods, before an entity is saved.

            This allows to make some changes to the instance before it's actually saved.
            The hook is provided with both the old and the new versions of the instance (!).

            :param new: The new instance
            :param prev: Previously persisted version (is provided only when updating).
        """
        pass

    # endregion

    # NOTE: there's no delete hook. Override _method_delete() to implement it.

    # ###
    # CRUD methods' implementations

    def _method_get(self, *filter, **filter_by) -> object:
        """ Fetch a single entity: as in READ, single entity

            Normally, used when the user has supplied a primary key:

                GET /users/1

            :param query_obj: Query Object
            :param filter: Additional filter() criteria
            :param filter_by: Additional filter_by() criteria
            :raises sqlalchemy.orm.exc.NoResultFound: Nothing found
            :raises sqlalchemy.orm.exc.MultipleResultsFound: Multiple found
            :raises exc.InvalidQueryError: Query Object errors made by the user
        """
        self._current_crud_method = CRUD_METHOD.GET
        instance = self._get_one(self._get_query_object(), *filter, **filter_by)
        return instance

    def _method_list(self, *filter, **filter_by) -> Iterable[object]:
        """ Fetch a list of entities: as in READ, list of entities

            Normally, used when the user has supplied no primary key:

                GET /users/

            NOTE: Be careful! This methods does not always return a list of entities!
            It can actually return:
            1. A scalar value: in case of a 'count' query
            2. A list of dicts: in case of an 'aggregate' or a 'group' query
            3. A list or entities: otherwise

            Please use the following MongoQuery methods to tell what's going on:
            MongoQuery.result_contains_entities(), MongoQuery.result_is_scalar(), MongoQuery.result_is_tuples()

            Or, else, override the following sub-methods:
            _method_list_result__entities(), _method_list_result__groups(), _method_list_result__count()

            :param query_obj: Query Object
            :param filter: Additional filter() criteria
            :param filter_by: Additional filter_by() criteria
            :raises exc.InvalidQueryError: Query Object errors made by the user
        """
        self._current_crud_method = CRUD_METHOD.LIST

        # Query
        query = self._mquery(self._get_query_object(), *filter, **filter_by)

        # Done
        return self._method_list_result_handler(query)

    def _method_list_result_handler(self, query: Query) -> Union[int, Iterable[object], Iterable[Tuple]]:
        """ Handle the results from method_list() """
        # Handle: Query Object has count
        if self._mongoquery.result_is_scalar():
            return self._method_list_result__count(query.scalar())

        # Handle: Query Object has group_by and yields tuples
        if self._mongoquery.result_is_tuples():
            # zip() column names together with the values,
            # and make it into a dict
            return self._method_list_result__groups(
                dict(zip(row.keys(), row))
                for row in query)  # return a generator

        # Regular result: entities
        return self._method_list_result__entities(iter(query))  # Return an iterable that yields entities, not a list

    def _method_list_result__entities(self, entities: Iterable[object]) -> Iterable[object]:
        """ Handle _method_list() result when it's a list of entities """
        return list(entities)  # because it may be an iterable

    def _method_list_result__groups(self, dicts: Iterable[dict]) -> Iterable[dict]:
        """ Handle _method_list() result when it's a list of dicts: the one you get from GROUP BY """
        return dicts

    def _method_list_result__count(self, n: int) -> int:
        """ Handle _method_list() result when it's an integer number: the one you get from COUNT() """
        return n

    def _method_create(self, entity_dict: dict) -> object:
        """ Create a new entity: as in CREATE

            Normally, used when the user has supplied no primary key:

                POST /users/
                {'name': 'Hakon'}

            :param entity_dict: Entity dict
            :return: The created instance (to be saved)
            :raises exc.InvalidQueryError: Query Object errors made by the user
        """
        self._current_crud_method = CRUD_METHOD.CREATE

        # Create a new instance
        # (wrapped with a relationship saver)
        instance = self._handle_saving_relationships(
            entity_dict,
            None,
            lambda entity_dict: self.crudhelper.create_model(entity_dict)
        )

        # Run the hook
        self._save_hook(instance, None)

        # Done
        # We don't save anything here
        return instance

    def _method_update(self, entity_dict: dict, *filter, **filter_by) -> object:
        """ Update an existing entity by merging the fields: as in UPDATE

            Normally, used when the user has supplied a primary key:

                POST /users/1
                {'id': 1, 'name': 'Hakon'}

            :param entity_dict: Entity dict
            :param filter: Criteria to find the previous entity
            :param filter_by: Criteria to find the previous entity
            :return: The updated instance (to be saved)
            :raises sqlalchemy.orm.exc.NoResultFound: The entity not found
            :raises sqlalchemy.orm.exc.MultipleResultsFound: Multiple entities found with the filter condition
            :raises exc.InvalidQueryError: Query Object errors made by the user
        """
        self._current_crud_method = CRUD_METHOD.UPDATE

        # Load the instance
        instance = self._get_one(self._get_query_object(), *filter, **filter_by)
        old_instance = ModelHistoryProxy(instance)

        # Update it
        # (wrapped with a relationship saver)
        instance = self._handle_saving_relationships(
            entity_dict,
            old_instance,
            lambda entity_dict: self.crudhelper.update_model(entity_dict, instance)
        )

        # Run the hook
        self._save_hook(instance, old_instance)

        # Done
        # We don't save anything here
        return instance

    def _method_delete(self, *filter, **filter_by) -> object:
        """ Delete an existing entity: as in DELETE

            Normally, used when the user has supplied a primary key:

                DELETE /users/1

            Note that it will load the entity from the database prior to deletion.

            :param filter: Criteria to find the previous entity
            :param filter_by: Criteria to find the previous entity
            :return: The instance to be deleted
            :raises sqlalchemy.orm.exc.NoResultFound: The entity not found
            :raises sqlalchemy.orm.exc.MultipleResultsFound: Multiple entities found with the filter condition
            :raises exc.InvalidQueryError: Query Object errors made by the user
        """
        self._current_crud_method = CRUD_METHOD.DELETE

        # Load
        instance = self._get_one(self._get_query_object(), *filter, **filter_by)

        # Return
        # We don't delete anything here
        return instance

    # region Helpers

    def _query(self) -> Query:
        """ Make the initial Query object to work with """
        return self._get_db_session().query(self.crudhelper.model)

    @property
    def _mongoquery(self) -> MongoQuery:
        """ Get the current MongoQuery for this request, or initialize a new one.

        :rtype: MongoQuery
        """
        # Init a new one, if necessary
        if not self.__mongoquery:
            # MongoQuery object is not explicitly created during CREATE requests
            # Therefore, we have to initialize it manually
            self.__mongoquery = self._mquery_simple(self._get_query_object())

        # Return
        return self.__mongoquery

    @_mongoquery.setter
    def _mongoquery(self, mongoquery: MongoQuery):
        self.__mongoquery = mongoquery
        return self.__mongoquery

    def _mquery_end(self, mongoquery: MongoQuery) -> Query:
        """ Finalize a MongoQuery and generate a Query """
        return mongoquery.end()

    def _mquery(self, query_object: Union[dict, None] = None, *filter, **filter_by) -> Query:
        """ Run a MongoQuery and invoke the View's hooks.

            This method is used by other methods to initialize all CRUD queries in this view.

            :param query_object: Query Object
            :param filter: Additional filter() criteria
            :param filter_by: Additional filter_by() criteria
            :raises exc.InvalidQueryError: Query Object errors made by the user
        """
        # Initialize the MongoQuery
        mquery = self._mquery_simple(query_object, *filter, **filter_by)

        # ensure_loaded(), when applicable
        if mquery.result_contains_entities():
            mquery.ensure_loaded(*self.ensure_loaded)

        # Session
        mquery.with_session(self._get_db_session())  # not really necessary, because _query() does it already

        # MongoQuery hook
        mquery = self._mongoquery_hook(mquery)

        # Store
        self._mongoquery = mquery

        # Query
        q = self._mquery_end(mquery)

        # Done
        return q

    def _mquery_simple(self, query_object: dict = None, *filter, **filter_by) -> MongoQuery:
        """ Use a MongoQuery to make a Query, with the Query Object, and initial custom filtering applied.

            This method does not run the View's hooks; that's why it is "simple".

            :param query_object: Query Object
            :type query_object: dict | None
            :param filter: Additional filter() criteria
            :param filter_by: Additional filter_by() criteria
            :raises exc.InvalidQueryError: Query Object errors made by the user
        """
        # We have to make a Query object and filter it in advance,
        # because later on, MongoSQL may put a LIMIT, or something worse, and no filter() will be possible anymore.
        q = self._query()

        # Filters: only apply when necessary
        if filter:
            q = q.filter(*filter)
        if filter_by:
            q = q.filter_by(**filter_by)

        # MongoQuery
        mquery = self.crudhelper.query_model(query_object, from_query=q)  # type: MongoQuery

        # Done
        return mquery

    def _get_one(self, query_obj: dict, *filter, **filter_by) -> object:
        """ Utility method that fetches a single entity.

            You will probably want to override it with custom error handling

            :param query_obj: Query Object
            :param filter: Additional filter() criteria
            :param filter_by: Additional filter_by() criteria
            :raises exc.InvalidQueryError: Query Object errors made by the user
            :raises sqlalchemy.orm.exc.NoResultFound: Nothing found
            :raises sqlalchemy.orm.exc.MultipleResultsFound: Multiple found
        """
        # Query
        query = self._mquery(query_obj, *filter, **filter_by)

        # Result
        return query.one()

    def _handle_saving_relationships(self, entity_dict: dict, prev_instance: object, wrapped_method: Callable) -> object:
        """ A helper wrapper that will save relationships of an instance while it's being created or updated.

            The idea is that this method will pluck `self.saves_relations` relatioships from the entity_dict,
            and then pass them to the `_save_relations()` handler.

            Args:
                entity_dict:
                    A dict that has come from the user that's going to save an entity
                old_instance:
                    The previous version of the instance (if available)
                wrapped_method:
                    The method wrapped with this helper.
        """
        # Pluck relations out of the entity dict
        relations_to_be_saved = {k: entity_dict.pop(k, None)
                                 for k in self.saves_relations}

        # Update it
        new_instance = wrapped_method(entity_dict)

        # Save relations
        if relations_to_be_saved:
            self._save_relations(new_instance, prev_instance, **relations_to_be_saved)

        # Done
        return new_instance

    # endregion
