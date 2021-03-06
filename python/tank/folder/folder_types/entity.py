# Copyright (c) 2013 Shotgun Software Inc.
#
# CONFIDENTIAL AND PROPRIETARY
#
# This work is provided "AS IS" and subject to the Shotgun Pipeline Toolkit
# Source Code License included in this distribution package. See LICENSE.
# By accessing, using, copying or modifying this work you indicate your
# agreement to the Shotgun Pipeline Toolkit Source Code License. All rights
# not expressly granted therein are reserved by Shotgun Software Inc.

import os
import copy

from ...errors import TankError
from ...util import shotgun_entity, login
from ...template import TemplatePath
from ...templatekey import StringKey

from .errors import EntityLinkTypeMismatch
from .base import Folder
from .expression_tokens import FilterExpressionToken
from .util import translate_filter_tokens, resolve_shotgun_filters


class Entity(Folder):
    """
    Represents an entity in Shotgun
    """

    @classmethod
    def create(cls, tk, parent, full_path, metadata):
        """
        Factory method for this class

        :param tk: Tk API instance
        :param parent: Parent :class:`Folder` object.
        :param full_path: Full path to the configuration file
        :param metadata: Contents of configuration file.
        :returns: :class:`Entity` instance.
        """
        # get data
        sg_name_expression = metadata.get("name")
        entity_type = metadata.get("entity_type")
        filters = metadata.get("filters")
        create_with_parent = metadata.get("create_with_parent", False)

        # validate
        if sg_name_expression is None:
            raise TankError("Missing name token in yml metadata file %s" % full_path)

        if entity_type is None:
            raise TankError("Missing entity_type token in yml metadata file %s" % full_path)

        if filters is None:
            raise TankError("Missing filters token in yml metadata file %s" % full_path)

        entity_filter = translate_filter_tokens(filters, parent, full_path)

        return Entity(
            tk,
            parent,
            full_path,
            metadata,
            entity_type,
            sg_name_expression,
            entity_filter,
            create_with_parent
        )

    def __init__(self, tk, parent, full_path, metadata, entity_type, field_name_expression, filters, create_with_parent):
        """
        Constructor.

        The filter syntax for deciding which folders to create
        is a dictionary, often looking something like this:

             {
                 "logical_operator": "and",
                 "conditions": [ { "path": "project", "relation": "is", "values": [ FilterExpressionToken(<Project>) ] } ]
             }

        This is basically a shotgun API filter dictionary, but with interleaved tokens
        (e.g. the FilterExpressionToken object). Tank will resolve any Token fields prior to
        passing the filter to Shotgun for evaluation.
        """
        self._entity_type = entity_type
        self._field_name = field_name_expression
        self._entity_expression = shotgun_entity.EntityExpression(tk, self._entity_type, self._field_name)
        self._filters = filters
        self._create_with_parent = create_with_parent

        # the schema name is the same as the SG entity type
        Folder.__init__(self, tk, parent, full_path, metadata)

    def _create_template_keys(self):
        """
        TemplateKey creation implementation. Implemented by all subclasses.
        """
        # Figure out which fields to create keys for
        fields = self._entity_expression.get_shotgun_fields()

        template_keys = []
        for field_name in fields:

            # Name the key using the entity_type and field_name to ensure uniqueness
            key_name = "%s.%s" % (self._entity_type, field_name)

            # If the key already exists, use it
            if key_name in self.template_keys:
                template_key = self.template_keys[key_name]

            # Else create an entity key for this pairing
            else:
                template_key = StringKey(key_name,
                                         self._tk.pipeline_configuration,
                                         shotgun_entity_type=self._entity_type,
                                         shotgun_field_name=field_name)
            # Add key to the list
            template_keys.append(template_key)

        return template_keys

    def _create_template_path(self):
        """
        Template path creation implementation. Implemented by all subclasses.

        Should return a TemplatePath object for the path of form: "{Project}/{Sequence}/{Shot}/user/{user_workspace}/{Step}"
        """
        # Generate the TemplatePath using the TemplateKeys collected earlier
        key_names = dict([(k.shotgun_field_name, "{%s}" % k.name) for k in self._template_keys])

        template_path = self._entity_expression.generate_name(key_names, validate=False)
        if self._parent:
            template_path = os.path.join(str(self._parent.template_path), template_path)

        return TemplatePath(template_path,
                            self.template_keys,
                            self._tk.pipeline_configuration,
                            self.get_storage_root(),
                            self.name)

    def get_entity_type(self):
        """
        returns the shotgun entity type for this node
        """
        return self._entity_type

    def _should_item_be_processed(self, engine_str, is_primary):
        """
        Checks if this node should be processed, given its deferred status.
        """
        # check our special condition - is this node set to be auto-created with its parent node?
        # note that primary nodes are always created with their parent nodes!
        if is_primary == False and self._create_with_parent == False:
            return False

        # base class implementation
        return super(Entity, self)._should_item_be_processed(engine_str, is_primary)

    def _get_additional_sg_fields(self):
        """
        Returns additional shotgun fields to be retrieved.

        Can be subclassed for special cases.

        :returns: List of shotgun fields to retrieve in addition to those
                  specified in the configuration files.
        """
        return []

    def _create_folders_impl(self, io_receiver, parent_path, sg_data):
        """
        Creates folders.
        """
        items_created = []

        for entity in self.__get_entities(sg_data):

            # generate the field name
            folder_name = self._entity_expression.generate_name(entity)

            # now for the case where the project name is encoded with slashes,
            # we need to translate those into a native representation
            folder_name = folder_name.replace("/", os.path.sep)

            my_path = os.path.join(parent_path, folder_name)

            # get the name field - which depends on the entity type
            # Note: this is the 'name' that will get stored in the path cache for this entity
            name_field = shotgun_entity.get_sg_entity_name_field(self._entity_type)
            name_value = entity[name_field]

            # construct a full entity link dict w name, id, type
            full_entity_dict = {"type": self._entity_type, "id": entity["id"], "name": name_value}

            # register secondary entity links
            self._register_secondary_entities(io_receiver, my_path, entity)

            # call out to callback
            io_receiver.make_entity_folder(my_path, full_entity_dict, self._config_metadata)

            # copy files across
            self._copy_files_to_folder(io_receiver, my_path)

            # create a new entity dict including our own data and pass it down to children
            my_sg_data = copy.deepcopy(sg_data)
            my_sg_data_key = FilterExpressionToken.sg_data_key_for_folder_obj(self)
            my_sg_data[my_sg_data_key].update(entity)
            my_sg_data[my_sg_data_key]["computed_name"] = folder_name

            # process symlinks
            self._process_symlinks(io_receiver, my_path, my_sg_data)

            items_created.append((my_path, my_sg_data))

        return items_created

    def _register_secondary_entities(self, io_receiver, path, entity):
        """
        Looks in the entity dict for any linked entities and register these
        """
        # get all the link fields from the name expression
        for lf in self._entity_expression.get_shotgun_link_fields():
            entity_link = entity[lf]
            io_receiver.register_secondary_entity(path, entity_link, self._config_metadata)

    def __get_entities(self, sg_data):
        """
        Returns shotgun data for folder creation
        """
        tokens = copy.deepcopy(sg_data)

        # first check the constraints: if tokens contains a type/id pair our our type,
        # we should only process this single entity. If not, then use the query filter
        filters = resolve_shotgun_filters(self._filters, tokens)

        # figure out which fields to retrieve
        fields = self._entity_expression.get_shotgun_fields()

        # add any shotgun link fields used in the expression
        fields.update(self._entity_expression.get_shotgun_link_fields())

        # always retrieve the name field for the entity
        fields.add(shotgun_entity.get_sg_entity_name_field(self._entity_type))

        # add any special stuff in
        fields.update(self._get_additional_sg_fields())

        # see if the sg_data dictionary has a "seed" entity type matching our entity type
        my_sg_data_key = FilterExpressionToken.sg_data_key_for_folder_obj(self)

        # First check to see if this is entity type is HumanUser
        if self._entity_type == "HumanUser":
            if my_sg_data_key not in tokens:
                # adds the current user to the filer query in case this has not already been done.
                # having this set up before the first call to create_folders rather than in the
                # constructor is partly for performance, but primarily so that a valid current user
                # isn't required unless you actually create a user sandbox folder. For example,
                # if you have a dedicated machine that creates higher level folders, this machine
                # shouldn't need to have a user id set up - only the artists that actually create
                # the user folders should need to.
                user = login.get_current_user(self._tk)
                if not user:
                    msg = ("Folder Creation Error: Could not find a HumanUser in shotgun with login "
                           "matching the local login. Check that the local login corresponds to a "
                           "user in shotgun.")
                    raise TankError(msg)

                tokens[my_sg_data_key] = user

        if my_sg_data_key in tokens:

            # If we have everything we need for this folder, just return
            if fields.issubset(tokens[my_sg_data_key]):
                return [tokens[my_sg_data_key]]

            # Else constrain the search to this entity
            else:
                entity_id = tokens[my_sg_data_key]["id"]
                filters["conditions"].append({"path": "id", "relation": "is", "values": [entity_id]})

        # now find all the items (e.g. shots) matching this query
        return self._tk.shotgun.find(self._entity_type, filters, list(fields))

    def extract_shotgun_data_upwards(self, sg, shotgun_data):
        """
        Extracts the shotgun data necessary to create this object and all its parents.
        The shotgun_data input needs to contain a dictionary with a "seed". For example:
        { "Shot": {"type": "Shot", "id": 1234 } }


        This method will then first extend this structure to ensure that fields needed for
        folder creation are available:
        { "Shot": {"type": "Shot", "id": 1234, "code": "foo", "sg_status": "ip" } }

        Now, if you have structure with Project > Sequence > Shot, the Shot level needs
        to define a configuration entry roughly on the form
        filters: [ { "path": "sg_sequence", "relation": "is", "values": [ "$sequence" ] } ]

        So in addition to getting the fields required for naming the current entry, we also
        get all the fields that are represented by $tokens. These will form the 'seed' for
        when we recurse to the parent level and do the same thing there.


        The return data is on the form:
        {
            'Project':   {'id': 4, 'name': 'Demo Project', 'type': 'Project'},
            'Sequence':  {'code': 'Sequence1', 'id': 2, 'name': 'Sequence1', 'type': 'Sequence'},
            'Shot':      {'code': 'shot_010', 'id': 2, 'type': 'Shot'}
        }

        NOTE! Because we are using a dictionary where we key by type, it would not be possible
        to have a pathway where the same entity type exists multiple times. For example an
        asset / sub asset relationship.
        """

        tokens = copy.deepcopy(shotgun_data)

        my_sg_data_key = FilterExpressionToken.sg_data_key_for_folder_obj(self)

        # First check to see if this is entity type is HumanUser
        if self._entity_type == "HumanUser":
            if my_sg_data_key not in tokens:
                # adds the current user to the filer query in case this has not already been done.
                # having this set up before the first call to create_folders rather than in the
                # constructor is partly for performance, but primarily so that a valid current user
                # isn't required unless you actually create a user sandbox folder. For example,
                # if you have a dedicated machine that creates higher level folders, this machine
                # shouldn't need to have a user id set up - only the artists that actually create
                # the user folders should need to.
                user = login.get_current_user(self._tk)
                if not user:
                    msg = ("Folder Creation Error: Could not find a HumanUser in shotgun with login "
                           "matching the local login. Check that the local login corresponds to a "
                           "user in shotgun.")
                    raise TankError(msg)

                tokens[my_sg_data_key] = user


        # If we don't have an entry in tokens for the current entity type, then we can't
        # extract any tokens. Used by #17726. Typically, we start with a "seed", and then go
        # upwards. For example, if the seed is a Shot id, we then scan upwards, look at the config
        # for shot, which contains [sg_sequence is $sequence], e.g. the shot entry links explicitly
        # to the sequence entry. Because of this link, by the time we move upwards in the hierarchy
        # and reach sequence, we will already have an entry for sequence in the dictionary.
        #
        # however, if we have a free-floating item in the hierarchy, this will not be 'seeded'
        # by its children as we move upwards - for example a step.
        if my_sg_data_key in tokens:

            # figure out which fields to retrieve
            fields = self._entity_expression.get_shotgun_fields()

            # add any shotgun link fields used in the expression
            fields.update(self._entity_expression.get_shotgun_link_fields())

            # add any special stuff in
            fields.update(self._get_additional_sg_fields())

            # always retrieve the name field for the entity
            name_field = shotgun_entity.get_sg_entity_name_field(self._entity_type)
            fields.add(name_field)

            link_map = {}
            filters = []

            # TODO: Support nested conditions
            for condition in self._filters["conditions"]:
                vals = condition["values"]
                if not len(vals):
                    continue

                # note the $FROM$ condition below - this is a bit of a hack to make sure we exclude
                # the special $FROM$ step based culling filter that is commonly used. Because steps are
                # sort of free floating and not associated with an entity, removing them from the
                # resolve should be fine in most cases.
                if condition["path"].startswith('$FROM$'):
                    continue

                # so - if at the shot level, we have defined the following filter:
                # filters: [ { "path": "sg_sequence", "relation": "is", "values": [ "$sequence" ] } ]
                # the $sequence will be represented by a Token object and we need to get a value for
                # this token. We fetch the id for this token and then, as we recurse upwards, and process
                # the parent folder level (the sequence), this id will be the "seed" when we populate that
                # level.
                if isinstance(vals[0], FilterExpressionToken):
                    # we should get this field (eg. 'sg_sequence')
                    fields.add(condition["path"])

                    # add to our map for later processing map['sg_sequence'] = 'Sequence'
                    # note that for List fields, the key is EntityType.field
                    link_map[condition["path"]] = vals[0]

                else:
                    # this is a normal filter - ex.) 'name must begin with X' - so we want
                    # to include these in the query where we are looking for the object, to
                    # ensure that assets with names starting with X are not created for an
                    # asset folder node which explicitly excludes these via its filters.
                    filters.append(condition)

            # If we don't have everything we need for this folder, go get it
            if not fields.issubset(tokens[my_sg_data_key]):

                # TODO: AND the id query with this folder's query to make sure this path is
                # valid for the current entity. Throw error if not so driver code knows to
                # stop processing. This would be needed in a setup where (for example) Asset
                # appears in several locations in the filesystem and that the filters are responsible
                # for determining which location to use for a particular asset.
                my_id = tokens[my_sg_data_key]["id"]
                filters.append({"path": "id", "relation": "is", "values": [my_id]})

                # append additional filter cruft
                filter_dict = { "logical_operator": "and", "conditions": filters }

                # carry out find
                rec = sg.find_one(self._entity_type, filter_dict, list(fields))

                # there are now two reasons why find_one did not return:
                # - the specified entity id does not exist or has been deleted
                # - there are filters which has filtered it out. For example imagine that you
                #   have one folder structure for all assets starting with A and a second structure
                #   for the rest. This would be a filter condition (code does not start with A, and
                #   code starts with A respectively). In these cases, the object does exist but has been
                #   explicitly filtered out - which is not an error!

                if not rec:
                    # check if it is a missing id or just a filtered out thing
                    if sg.find_one(self._entity_type, [["id", "is", my_id]]) is None:
                        raise TankError("Could not find Shotgun %s with id %s as required by "
                                        "the folder creation setup." % (self._entity_type, my_id))
                    else:
                        raise EntityLinkTypeMismatch()

                # Update the tokens for this entity
                tokens[my_sg_data_key].update(rec)

            # Step through our token key map and see if we can promote
            # any linked fields to top-level "seed" entities for the next
            # level of recursion
            #
            # This is of the form
            # link_map['sg_sequence'] = link_obj
            #
            for field in link_map:
                # do some juggling to make sure we don't double process the
                # name fields.
                link_obj = link_map[field]

                # Get the linked entity data key
                data_key = link_obj.get_sg_data_key()

                value = tokens[my_sg_data_key].get(field)
                if value is None:
                    # field was none! - cannot handle that!
                    raise EntityLinkTypeMismatch("The %s %s has a required field %s that "
                        "does not have a value set in Shotgun. Double check the values "
                        "and try again!" % (self._entity_type, name_field, field))

                # store it in our sg_data prefetch chunk
                if isinstance(value, dict):
                    # If the value is a dict, assume it comes from an entity,
                    # so make sure that this link is actually relevant for us,
                    # e.g. that it points to an entity of the right type.
                    # this may be a problem whenever a link can link to more
                    # than one type. See the EntityLinkTypeMismatch docs for example.
                    if value["type"] != link_obj.get_entity_type():
                        raise EntityLinkTypeMismatch()

                    if data_key not in tokens:
                        tokens[data_key] = {}
                    tokens[data_key].update(value)

                elif isinstance(value, list):
                    if data_key not in tokens:
                        tokens[data_key] = []
                    tokens[data_key].extend(value)

                else:
                    tokens[data_key] = value

        # now keep recursing upwards
        if self._parent:
            return self._parent.extract_shotgun_data_upwards(sg, tokens)

        return tokens

    def get_entries_from_path(self, input_path):
        """
        folder_obj = $project/$sequence/$shot/user/work.$login/$step
        """
        # Parse the input path using the folder_obj's template
        path_fields = self.template_path.get_fields(input_path)

        # Create a list of folder objs to recurse over, including self
        folder_objs_to_recurse = [self] + self.get_parents()

        sg_data = {}
        primary_entry = None
        entries = []
        secondary_entries = []

        # Recurse "bottom up" to derive all requisite entities and links
        for folder_obj in folder_objs_to_recurse[::-1]:

            try:
                entity = folder_obj.get_entity_from_fields(path_fields, sg_data)
            except AttributeError:
                # This is not an Entity folder_obj...that's ok, moving on
                continue

            # get the name field - which depends on the entity type
            # Note: this is the 'name' that will get stored in the path cache for this entity
            name_field = shotgun_entity.get_sg_entity_name_field(entity["type"])
            name_value = entity[name_field]

            # construct a full entity link dict w name, id, type
            entity_dict = {"type": entity["type"], "id": entity["id"], "name": name_value}

            # Derive the local path for this folder_obj
            local_path = folder_obj.template_path.apply_fields(path_fields)

            # Add entity to entries list
            entries.append({
                "entity": entity_dict,
                "path": local_path,
                "primary": True,
                "metadata": self._config_metadata
            })

            # Register any secondary entity links
            for lf in self._entity_expression.get_shotgun_link_fields():
                entity_link = entity[lf]
                secondary_entries.append({
                    "entity": entity_link,
                    "path": local_path,
                    "primary": False,
                    "metadata": self._config_metadata
                })

        # Since we recursed backwards, the last element is the primary entity
        # corresponding to this folder obj, so pop it off the end of the list
        if entries:
            primary_entry = entries.pop()

        return primary_entry, entries + secondary_entries

    def get_entity_from_fields(self, path_fields, sg_data):
        """
        Returns a SG entity matching this folder's entity_type and the matching
        value stored in the input path_fields. Also uses sg_data for any
        additional filtering.
        """
        # Get the corresponding entity type for this folder obj
        entity_type = self.get_entity_type()

        # Find the corresponding field name and path_field value matching this entity_type
        field_name = None
        field_value = None
        for field_key in path_fields.keys():
            if field_key in self.template_path.keys:
                template_key = self.template_path.keys[field_key]
                if template_key.shotgun_entity_type == entity_type:
                    field_name = template_key.shotgun_field_name
                    field_value = path_fields[field_key]
                    break

        # Confirm there is a value in the path for the entity type
        if not field_name:
            raise TankError("Entity type '%s' missing from path fields." % entity_type)

        # Resolve any filters
        resolved_filters = resolve_shotgun_filters(self._filters, sg_data)

        # Do lookup by corresponding field name
        resolved_filters["conditions"].append({ "path": field_name, "relation": "is", "values": [field_value] })

        # figure out which fields to retrieve
        fields = self._entity_expression.get_shotgun_fields()

        # add any shotgun link fields used in the expression
        fields.update(self._entity_expression.get_shotgun_link_fields())

        # always retrieve the name field for the entity
        name_field = shotgun_entity.get_sg_entity_name_field(entity_type)
        fields.add(name_field)

        # add any special stuff in
        fields.update(self._get_additional_sg_fields())

        # now find the item matching this query
        entity = self._tk.shotgun.find_one(entity_type, resolved_filters, list(fields))
        if not entity:
            raise TankError("Cannot find %s Entity: '%s' in Shotgun using filter: %s"
                    % (entity_type, field_value, resolved_filters))

        # Add this entity to the sg_data dict for further processing
        sg_data[entity_type] = entity

        # Finally return this entity
        return entity
