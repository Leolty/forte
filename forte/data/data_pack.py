# Copyright 2019 The Forte Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import logging
from pathlib import Path
from typing import (
    Dict,
    Iterable,
    Iterator,
    List,
    Optional,
    Type,
    Union,
    Any,
    Set,
    Callable,
    Tuple,
)
from functools import partial
from typing_inspect import get_origin
from packaging.version import Version
import numpy as np
from sortedcontainers import SortedList

from forte.common.exception import (
    ProcessExecutionException,
    UnknownOntologyClassException,
)
from forte.common.constants import TID_INDEX
from forte.data import data_utils_io
from forte.data.data_store import DataStore
from forte.data.base_pack import BaseMeta, BasePack
from forte.data.index import BaseIndex
from forte.data.ontology.core import Entry, FList, FDict
from forte.data.ontology.core import EntryType
from forte.data.ontology.top import (
    Annotation,
    Link,
    Group,
    SinglePackEntries,
    Generics,
    AudioAnnotation,
    ImageAnnotation,
    Grids,
)
from forte.data.span import Span
from forte.data.types import ReplaceOperationsType, DataRequest
from forte.utils import get_class, get_full_module_name
from forte.version import PACK_ID_COMPATIBLE_VERSION, DEFAULT_PACK_VERSION

logger = logging.getLogger(__name__)

__all__ = ["Meta", "DataPack", "DataIndex"]


class Meta(BaseMeta):
    r"""Basic Meta information associated with each instance of
    :class:`~forte.data.data_pack.DataPack`.

    Args:
        pack_name:  An name to identify the data pack, which is helpful in
           situation like serialization. It is suggested that the packs should
           have different doc ids.
        language: The language used by this data pack, default is English.
        span_unit: The unit used for interpreting the Span object of this
          data pack. Default is character.
        sample_rate: An integer specifying the sample rate of audio payload.
          Default is None.
        info: Store additional string based information that the user add.
    Attributes:
        pack_name:  storing the provided `pack_name`.
        language: storing the provided `language`.
        sample_rate: storing the provided `sample_rate`.
        info: storing the provided `info`.
        record: Initialized as a dictionary. This is not a required field.
            The key of the record should be the entry type and values should
            be attributes of the entry type. All the information would be used
            for consistency checking purpose if the pipeline is initialized with
            `enforce_consistency=True`.
    """

    def __init__(
        self,
        pack_name: Optional[str] = None,
        language: str = "eng",
        span_unit: str = "character",
        sample_rate: Optional[int] = None,
        info: Optional[Dict[str, str]] = None,
    ):
        super().__init__(pack_name)
        self.language = language
        self.span_unit = span_unit
        self.sample_rate: Optional[int] = sample_rate
        self.record: Dict[str, Set[str]] = {}
        self.info: Dict[str, str]
        if info is None:
            self.info = {}
        else:
            self.info = info


def as_entry_type(entry_type: Union[str, Type[EntryType]]):
    entry_type_: Type[EntryType]
    if isinstance(entry_type, str):
        entry_type_ = get_class(entry_type)
        if not issubclass(entry_type_, Entry):
            raise ValueError(
                f"The specified entry type [{entry_type}] "
                f"does not correspond to a "
                f"`forte.data.ontology.core.Entry` class"
            )
    else:
        entry_type_ = entry_type
    return entry_type_


def as_sorted_error_check(entries: List[EntryType]) -> SortedList:
    """
    Given a list of entries, return a sorted list of it. If unknown entry
    classes are seen during this process,
    a :class:`~forte.common.exception.UnknownOntologyClassException` exception will be
    thrown.

    Args:
        entries: A list of entries to be converted.

    Returns: Sorted list of the input entries.
    """
    try:
        return SortedList(entries)
    except TypeError as e:
        for entry in entries:
            if isinstance(entry, Dict) and "py/object" in entry:
                entry_class = entry["py/object"]
                try:
                    get_class(entry_class)
                except ValueError:
                    raise UnknownOntologyClassException(
                        f"Cannot deserialize ontology type {entry_class}, "
                        f"make sure it is included in the PYTHONPATH."
                    ) from e


class DataPack(BasePack[Entry, Link, Group]):
    # pylint: disable=too-many-public-methods, unused-private-member
    r"""A :class:`~forte.data.data_pack.DataPack` contains a piece of natural language text and a
    collection of NLP entries (annotations, links, and groups). The natural
    language text could be a document, paragraph or in any other granularity.

    Args:
        pack_name: A name for this data pack.
    """

    def __init__(self, pack_name: Optional[str] = None):
        super().__init__(pack_name)
        self._text = ""
        self._audio: Optional[np.ndarray] = None

        self._data_store: DataStore = DataStore()
        self._entry_converter: EntryConverter = EntryConverter()
        self.image_annotations: List[ImageAnnotation] = []
        self.grids: List[Grids] = []
        self.payloads: List[np.ndarray] = []

        self.__replace_back_operations: ReplaceOperationsType = []
        self.__processed_original_spans: List[Tuple[Span, Span]] = []

        self.__orig_text_len: int = 0

        self._index: DataIndex = DataIndex()

    def __getstate__(self):
        r"""
        In serialization,
            1) will remove ``_entry_converter`` to save space.
        """
        state = super().__getstate__()
        state.pop("_entry_converter")
        return state

    def __setstate__(self, state):
        r"""
        In deserialization, we
            1) Perform pack version compatibility checking;
            2) initialize the entry converter
            3) initialize the indexes.
            4) Obtain the pack ids.
        """
        # Pack version checking. We will no longer provide support for
        # serialized DataPack whose "pack_version" is less than
        # PACK_ID_COMPATIBLE_VERSION.
        pack_version: str = (
            state["pack_version"]
            if "pack_version" in state
            else DEFAULT_PACK_VERSION
        )
        if Version(pack_version) < Version(PACK_ID_COMPATIBLE_VERSION):
            raise ValueError(
                "The DataPack cannot be deserialized because its version "
                f"{pack_version} is outdated. We only support DataPack with "
                f"version greater or equal to {PACK_ID_COMPATIBLE_VERSION}"
            )

        self._entry_converter = EntryConverter()
        super().__setstate__(state)

        # For backward compatibility.
        if "replace_back_operations" in self.__dict__:
            self.__replace_back_operations = self.__dict__.pop(
                "replace_back_operations"
            )
        if "processed_original_spans" in self.__dict__:
            self.__processed_original_spans = self.__dict__.pop(
                "processed_original_spans"
            )
        if "orig_text_len" in self.__dict__:
            self.__orig_text_len = self.__dict__.pop("orig_text_len")

        self._index = DataIndex()
        self._index.update_basic_index(list(iter(self)))

    def __iter__(self):
        yield from self.annotations
        yield from self.links
        yield from self.groups
        yield from self.generics
        yield from self.audio_annotations

    def _init_meta(self, pack_name: Optional[str] = None) -> Meta:
        return Meta(pack_name)

    def _validate(self, entry: EntryType) -> bool:
        return isinstance(entry, SinglePackEntries)

    @property
    def text(self) -> str:
        r"""Return the text of the data pack"""
        return self._text

    @property
    def audio(self) -> Optional[np.ndarray]:
        r"""Return the audio of the data pack"""
        return self._audio

    @property
    def sample_rate(self) -> Optional[int]:
        r"""Return the sample rate of the audio data"""
        return getattr(self._meta, "sample_rate")

    @property
    def all_annotations(self) -> Iterator[Annotation]:
        """
        An iterator of all annotations in this data pack.

        Returns: Iterator of all annotations, of
        type :class:`~forte.data.ontology.top.Annotation`.

        """
        for entry in self._data_store.all_entries(
            "forte.data.ontology.top.Annotation"
        ):
            yield self.get_entry(tid=entry[TID_INDEX])

    @property
    def num_annotations(self) -> int:
        """
        Number of annotations in this data pack.

        Returns: (int) Number of the links.

        """
        return self._data_store.num_entries(
            "forte.data.ontology.top.Annotation"
        )

    @property
    def all_links(self) -> Iterator[Link]:
        """
        An iterator of all links in this data pack.

        Returns: Iterator of all links, of
        type :class:`~forte.data.ontology.top.Link`.

        """
        for entry in self._data_store.all_entries(
            "forte.data.ontology.top.Link"
        ):
            yield self.get_entry(tid=entry[TID_INDEX])

    @property
    def num_links(self) -> int:
        """
        Number of links in this data pack.

        Returns: Number of the links.

        """
        return self._data_store.num_entries("forte.data.ontology.top.Link")

    @property
    def all_groups(self) -> Iterator[Group]:
        """
        An iterator of all groups in this data pack.

        Returns: Iterator of all groups, of
        type :class:`~forte.data.ontology.top.Group`.

        """
        for entry in self._data_store.all_entries(
            "forte.data.ontology.top.Group"
        ):
            yield self.get_entry(tid=entry[TID_INDEX])

    @property
    def num_groups(self):
        """
        Number of groups in this data pack.

        Returns: Number of groups.

        """
        return self._data_store.num_entries("forte.data.ontology.top.Group")

    @property
    def all_generic_entries(self) -> Iterator[Generics]:
        """
        An iterator of all generic entries in this data pack.

        Returns: Iterator of generic

        """
        for entry in self._data_store.all_entries(
            "forte.data.ontology.top.Generics"
        ):
            yield self.get_entry(tid=entry[TID_INDEX])

    @property
    def num_generics_entries(self):
        """
        Number of generics entries in this data pack.

        Returns: Number of generics entries.

        """
        return self._data_store.num_entries("forte.data.ontology.top.Generics")

    @property
    def all_audio_annotations(self) -> Iterator[AudioAnnotation]:
        """
        An iterator of all audio annotations in this data pack.

        Returns: Iterator of all audio annotations, of
        type :class:`~forte.data.ontology.top.AudioAnnotation`.

        """
        for entry in self._data_store.all_entries(
            "forte.data.ontology.top.AudioAnnotation"
        ):
            yield self.get_entry(tid=entry[TID_INDEX])

    @property
    def num_audio_annotations(self):
        """
        Number of audio annotations in this data pack.

        Returns: Number of audio annotations.

        """
        return self._data_store.num_entries(
            "forte.data.ontology.top.AudioAnnotation"
        )

    @property
    def annotations(self):
        """
        A SortedList container of all annotations in this data pack.

        Returns: SortedList of all annotations, of
        type :class:`~forte.data.ontology.top.Annotation`.

        """
        return SortedList(self.all_annotations)

    @property
    def generics(self):
        """
        A SortedList container of all generic entries in this data pack.

        Returns: SortedList of generics

        """
        return SortedList(self.all_generic_entries)

    @property
    def audio_annotations(self):
        """
        A SortedList container of all audio annotations in this data pack.

        Returns: SortedList of all audio annotations, of
        type :class:`~forte.data.ontology.top.AudioAnnotation`.

        """
        return SortedList(self.all_audio_annotations)

    @property  # type: ignore
    def links(self):
        """
        A List container of all links in this data pack.

        Returns: List of all links, of
        type :class:`~forte.data.ontology.top.Link`.

        """
        # TODO: Right now we create a new variable `_links` here to avoid
        # conflicts from BasePack and MultiPack. After DataStore is fully
        # integrated with MultiPack, we should reconsider the design here.
        if isinstance(self, DataPack):
            self._links = SortedList(self.all_links)
        return self._links

    @links.setter
    def links(self, val):
        self._links = val

    @property  # type: ignore
    def groups(self):
        """
        A List container of all groups in this data pack.

        Returns: List of all groups, of
        type :class:`~forte.data.ontology.top.Group`.

        """
        # TODO: Right now we create a new variable `_groups` here to avoid
        # conflicts from BasePack and MultiPack. After DataStore is fully
        # integrated with MultiPack, we should reconsider the design here.
        if isinstance(self, DataPack):
            self._groups = SortedList(self.all_links)
        return self._groups

    @groups.setter
    def groups(self, val):
        self._groups = val

    def get_span_text(self, begin: int, end: int) -> str:
        r"""Get the text in the data pack contained in the span.

        Args:
            begin: begin index to query.
            end: end index to query.

        Returns:
            The text within this span.
        """
        return self._text[begin:end]

    def get_span_audio(self, begin: int, end: int) -> np.ndarray:
        r"""Get the audio in the data pack contained in the span.
        `begin` and `end` represent the starting and ending indices of the span
        in audio payload respectively. Each index corresponds to one sample in
        audio time series.

        Args:
            begin: begin index to query.
            end: end index to query.

        Returns:
            The audio within this span.
        """
        if self._audio is None:
            raise ProcessExecutionException(
                "The audio payload of this DataPack is not set. Please call"
                " method `set_audio` before running `get_span_audio`."
            )
        return self._audio[begin:end]

    def get_image_array(self, image_payload_idx: int):
        if image_payload_idx >= len(self.payloads):
            raise ValueError(
                f"The input image payload index{(image_payload_idx)}"
                f" out of range. It should be less than {len(self.payloads)}"
            )
        return self.payloads[image_payload_idx]

    def set_text(
        self,
        text: str,
        replace_func: Optional[Callable[[str], ReplaceOperationsType]] = None,
    ):

        if len(text) < len(self._text):
            raise ProcessExecutionException(
                "The new text is overwriting the original one with shorter "
                "length, which might cause unexpected behavior."
            )

        if len(self._text):
            logging.warning(
                "Need to be cautious when changing the text of a "
                "data pack, existing entries may get affected. "
            )

        span_ops = [] if replace_func is None else replace_func(text)

        # The spans should be mutually exclusive
        (
            self._text,
            self.__replace_back_operations,
            self.__processed_original_spans,
            self.__orig_text_len,
        ) = data_utils_io.modify_text_and_track_ops(text, span_ops)

    def set_audio(self, audio: np.ndarray, sample_rate: int):
        r"""Set the audio payload and sample rate of the :class:`~forte.data.data_pack.DataPack`
        object.

        Args:
            audio: A numpy array storing the audio waveform.
            sample_rate: An integer specifying the sample rate.
        """
        self._audio = audio
        self.set_meta(sample_rate=sample_rate)

    def get_original_text(self):
        r"""Get original unmodified text from the :class:`~forte.data.data_pack.DataPack` object.

        Returns:
            Original text after applying the `replace_back_operations` of
            :class:`~forte.data.data_pack.DataPack` object to the modified text
        """
        original_text, _, _, _ = data_utils_io.modify_text_and_track_ops(
            self._text, self.__replace_back_operations
        )
        return original_text

    def get_original_span(
        self, input_processed_span: Span, align_mode: str = "relaxed"
    ):
        r"""Function to obtain span of the original text that aligns with the
        given span of the processed text.

        Args:

            input_processed_span: Span of the processed text for which
                the corresponding span of the original text is desired.
            align_mode: The strictness criteria for alignment in the
                ambiguous cases, that is, if a part of input_processed_span
                spans a part of the inserted span, then align_mode controls
                whether to use the span fully or ignore it completely according
                to the following possible values:

                    - "strict" - do not allow ambiguous input, give ValueError.
                    - "relaxed" - consider spans on both sides.
                    - "forward" - align looking forward, that is, ignore the
                      span towards the left, but consider the span towards
                      the right.
                    - "backward" - align looking backwards, that is, ignore the
                      span towards the right, but consider the span towards the
                      left.


        Returns:
            Span of the original text that aligns with input_processed_span

        Example:
            * Let o-up1, o-up2, ... and m-up1, m-up2, ... denote the unprocessed
              spans of the original and modified string respectively. Note that
              each o-up would have a corresponding m-up of the same size.
            * Let o-pr1, o-pr2, ... and m-pr1, m-pr2, ... denote the processed
              spans of the original and modified string respectively. Note that
              each o-p is modified to a corresponding m-pr that may be of a
              different size than o-pr.
            * Original string:
              <--o-up1--> <-o-pr1-> <----o-up2----> <----o-pr2----> <-o-up3->
            * Modified string:
              <--m-up1--> <----m-pr1----> <----m-up2----> <-m-pr2-> <-m-up3->
            * Note that `self.inverse_original_spans` that contains modified
              processed spans and their corresponding original spans, would look
              like - [(o-pr1, m-pr1), (o-pr2, m-pr2)]

        .. code-block:: python

            >> data_pack = DataPack()
            >> original_text = "He plays in the park"
            >> data_pack.set_text(original_text,\
            >>                    lambda _: [(Span(0, 2), "She"))]
            >> data_pack.text
            "She plays in the park"
            >> input_processed_span = Span(0, len("She plays"))
            >> orig_span = data_pack.get_original_span(input_processed_span)
            >> data_pack.get_original_text()[orig_span.begin: orig_span.end]
            "He plays"

        """
        assert align_mode in ["relaxed", "strict", "backward", "forward"]

        req_begin = input_processed_span.begin
        req_end = input_processed_span.end

        def get_original_index(
            input_index: int, is_begin_index: bool, mode: str
        ) -> int:
            r"""
            Args:
                input_index: begin or end index of the input span
                is_begin_index: if the index is the begin index of the input
                span or the end index of the input span
                mode: alignment mode
            Returns:
                Original index that aligns with input_index
            """
            if len(self.__processed_original_spans) == 0:
                return input_index

            len_processed_text = len(self._text)
            orig_index = None
            prev_end = 0
            for (
                inverse_span,
                original_span,
            ) in self.__processed_original_spans:
                # check if the input_index lies between one of the unprocessed
                # spans
                if prev_end <= input_index < inverse_span.begin:
                    increment = original_span.begin - inverse_span.begin
                    orig_index = input_index + increment
                # check if the input_index lies between one of the processed
                # spans
                elif inverse_span.begin <= input_index < inverse_span.end:
                    # look backward - backward shift of input_index
                    if is_begin_index and mode in ["backward", "relaxed"]:
                        orig_index = original_span.begin
                    if not is_begin_index and mode == "backward":
                        orig_index = original_span.begin - 1

                    # look forward - forward shift of input_index
                    if is_begin_index and mode == "forward":
                        orig_index = original_span.end
                    if not is_begin_index and mode in ["forward", "relaxed"]:
                        orig_index = original_span.end - 1

                # break if the original index is populated
                if orig_index is not None:
                    break
                prev_end = inverse_span.end

            if orig_index is None:
                # check if the input_index lies between the last unprocessed
                # span
                inverse_span, original_span = self.__processed_original_spans[
                    -1
                ]
                if inverse_span.end <= input_index < len_processed_text:
                    increment = original_span.end - inverse_span.end
                    orig_index = input_index + increment
                else:
                    # check if there input_index is not valid given the
                    # alignment mode or lies outside the processed string
                    raise ValueError(
                        f"The input span either does not adhere "
                        f"to the {align_mode} alignment mode or "
                        f"lies outside to the processed string."
                    )
            return orig_index

        orig_begin = get_original_index(req_begin, True, align_mode)
        orig_end = get_original_index(req_end - 1, False, align_mode) + 1

        return Span(orig_begin, orig_end)

    @classmethod
    def deserialize(
        cls,
        data_source: Union[Path, str],
        serialize_method: str = "jsonpickle",
        zip_pack: bool = False,
    ) -> "DataPack":
        """
        Deserialize a Data Pack from a string. This internally calls the
        internal :meth:`~forte.data.base_pack.BasePack._deserialize()` function
        from :class:`~forte.data.base_pack.BasePack`.

        Args:
            data_source: The path storing data source.
            serialize_method: The method used to serialize the data, this
              should be the same as how serialization is done. The current
              options are `jsonpickle` and `pickle`. The default method
              is `jsonpickle`.
            zip_pack: Boolean value indicating whether the input source is
              zipped.

        Returns:
            An data pack object deserialized from the string.
        """
        return cls._deserialize(data_source, serialize_method, zip_pack)

    def _add_entry(self, entry: Union[EntryType, int]) -> EntryType:
        r"""Force add an :class:`~forte.data.ontology.core.Entry` object to the
        :class:`~forte.data.data_pack.DataPack` object. Allow duplicate entries in a pack.

        Args:
            entry: An :class:`~forte.data.ontology.core.Entry`
                object to be added to the pack.

        Returns:
            The input entry itself
        """
        return self.__add_entry_with_check(entry)

    def __add_entry_with_check(self, entry: Union[EntryType, int]) -> EntryType:
        r"""Internal method to add an :class:`~forte.data.ontology.core.Entry`
        object to the :class:`~forte.data.DataPack` object.

        Args:
            entry: An :class:`~forte.data.ontology.core.Entry` object
                to be added to the datapack.
            allow_duplicate: Whether we allow duplicate in the datapack.

        Returns:
            The input entry itself
        """
        if isinstance(entry, int):
            # If entry is a TID, convert it to the class object.
            entry = self._entry_converter.get_entry_object(tid=entry, pack=self)

        if isinstance(entry, Annotation):
            begin, end = entry.begin, entry.end

            if begin < 0:
                raise ValueError(
                    f"The begin {begin} is smaller than 0, this"
                    f"is not a valid begin."
                )

            if end > len(self.text):
                if len(self.text) == 0:
                    raise ValueError(
                        f"The end {end} of span is greater than the text "
                        f"length {len(self.text)}, which is invalid. The text "
                        f"length is 0, so it may be the case the you haven't "
                        f"set text for the data pack. Please set the text "
                        f"before calling `add_entry` on the annotations."
                    )
                else:
                    pack_ref = entry.pack.pack_id
                    raise ValueError(
                        f"The end {end} of span is greater than the text "
                        f"length {len(self.text)}, which is invalid. The "
                        f"problematic entry is of type {entry.__class__} "
                        f"at [{begin}:{end}], in pack {pack_ref}."
                    )

        # update the data pack index if needed
        # TODO: DataIndex will be deprecated in future
        self._index.update_basic_index([entry])
        if self._index.link_index_on and isinstance(entry, Link):
            self._index.update_link_index([entry])
        if self._index.group_index_on and isinstance(entry, Group):
            self._index.update_group_index([entry])
        self._index.deactivate_coverage_index()
        self._pending_entries.pop(entry.tid)
        return entry  # type: ignore

    def delete_entry(self, entry: EntryType):
        r"""Delete an :class:`~forte.data.ontology.core.Entry` object from the
        :class:`~forte.data.data_pack.DataPack`. This find out the entry in the index and remove it
        from the index. Note that entries will only appear in the index if
        `add_entry` (or _add_entry_with_check) is called.

        Please note that deleting a entry do not guarantee the deletion of
        the related entries.

        Args:
            entry: An :class:`~forte.data.ontology.core.Entry`
                object to be deleted from the pack.

        """
        self._data_store.delete_entry(tid=entry.tid)

        # update basic index
        self._index.remove_entry(entry)

        # set other index invalid
        self._index.turn_link_index_switch(on=False)
        self._index.turn_group_index_switch(on=False)
        self._index.deactivate_coverage_index()

    @classmethod
    def validate_link(cls, entry: EntryType) -> bool:
        return isinstance(entry, Link)

    @classmethod
    def validate_group(cls, entry: EntryType) -> bool:
        return isinstance(entry, Group)

    def get_data(
        self,
        context_type: Union[str, Type[Annotation], Type[AudioAnnotation]],
        request: Optional[DataRequest] = None,
        skip_k: int = 0,
    ) -> Iterator[Dict[str, Any]]:
        r"""Fetch data from entries in the data_pack of type
        `context_type`. Data includes `"span"`, annotation-specific
        default data fields and specific data fields by `"request"`.

        Annotation-specific data fields means:

            - `"text"` for ``Type[Annotation]``
            - `"audio"` for ``Type[AudioAnnotation]``

        Currently, we do not support Groups and Generics in the request.

        Example:

            .. code-block:: python

                requests = {
                    base_ontology.Sentence:
                        {
                            "component": ["dummy"],
                            "fields": ["speaker"],
                        },
                    base_ontology.Token: ["pos", "sense"],
                    base_ontology.EntityMention: {
                    },
                }
                pack.get_data(base_ontology.Sentence, requests)

        Args:
            context_type:
                The granularity of the data context, which
                could be any :class:`~forte.data.ontology.top.Annotation` or
                :class:`~forte.data.ontology.top.AudioAnnotation` type.
                Behaviors under different context_type varies:

                - str type will be converted into either
                  :class:`~forte.data.ontology.top.Annotation` type or
                  :class:`~forte.data.ontology.top.AudioAnnotation` type.
                - ``Type[Annotation]``: the default data field for getting
                  context data is :attr:`text`. This function iterates
                  :attr:`all_annotations` to search target entry data.
                - ``Type[AudioAnnotation]``: the default data field for getting
                  context data is :attr:`audio` which stores audio data in
                  numpy arrays. This function iterates
                  :attr:`all_audio_annotations` to search target entry data.

            request: The
                entry types and fields User wants to request.
                The keys of the requests dict are the required entry types
                and the value should be either:

                - a list of field names or
                - a dict which accepts three keys: `"fields"`, `"component"`,
                  and `"unit"`.

                    - By setting `"fields"` (list), users
                      specify the requested fields of the entry. If "fields"
                      is not specified, only the default fields will be
                      returned.
                    - By setting `"component"` (list), users
                      can specify the components by which the entries are
                      generated. If `"component"` is not specified, will return
                      entries generated by all components.
                    - By setting `"unit"` (string), users can
                      specify a unit by which the annotations are indexed.

                Note that for all annotation types, `"span"`
                fields and annotation-specific data fields are returned by
                default.

                For all link types, `"child"` and `"parent"` fields are
                returned by default.
            skip_k: Will skip the first `skip_k` instances and generate
                data from the (`offset` + 1)th instance.

        Returns:
            A data generator, which generates one piece of data (a dict
            containing the required entries, fields, and context).
        """
        context_type_: Union[Type[Annotation], Type[AudioAnnotation]]
        if isinstance(context_type, str):
            context_type_ = get_class(context_type)
            if not issubclass(context_type_, Entry):
                raise ValueError(
                    f"The provided `context_type` [{context_type_}] "
                    f"is not a subclass to the"
                    f"`forte.data.ontology.top.Annotation` class"
                )
        else:
            context_type_ = context_type

        annotation_types: Dict[
            Union[Type[Annotation], Type[AudioAnnotation]], Union[Dict, List]
        ] = {}
        link_types: Dict[Type[Link], Union[Dict, List]] = {}
        group_types: Dict[Type[Group], Union[Dict, List]] = {}
        generics_types: Dict[Type[Generics], Union[Dict, List]] = {}
        audio_annotation_types: Dict[
            Type[AudioAnnotation], Union[Dict, List]
        ] = {}

        if request is not None:
            for key_, value in request.items():
                key = as_entry_type(key_)
                if issubclass(key, Annotation):
                    annotation_types[key] = value
                elif issubclass(key, Link):
                    link_types[key] = value
                elif issubclass(key, Group):
                    group_types[key] = value
                elif issubclass(key, Generics):
                    generics_types[key] = value
                elif issubclass(key, AudioAnnotation):
                    audio_annotation_types[key] = value

        context_args = annotation_types.get(context_type_)

        context_components, _, context_fields = self._parse_request_args(
            context_type_, context_args
        )

        valid_context_ids: Set[int] = self._index.query_by_type_subtype(
            context_type_
        )

        if context_components:
            valid_component_id: Set[int] = set()
            for component in context_components:
                valid_component_id |= self.get_ids_by_creator(component)
            valid_context_ids &= valid_component_id

        def get_annotation_list(
            c_type: Union[Type[Annotation], Type[AudioAnnotation]]
        ):
            r"""Get an annotation list of a given context type.

            Args:
                c_type:
                    The granularity of the data context, which
                    could be any :class:`~forte.data.ontology.top.Annotation` type.

            Raises:
                NotImplementedError: raised when the given context type is
                    not implemented.

            Returns:
                List(Union[Annotation, AudioAnnotation]):
                    a list of annotations which is a copy of `self.annotations`
                    and it enables modifications of `self.annotations` while
                    iterating through its copy.
            """
            if issubclass(c_type, Annotation):
                return list(self.annotations)
            elif issubclass(c_type, AudioAnnotation):
                return list(self.audio_annotations)
            else:
                raise NotImplementedError(
                    f"Context type is set to {c_type},"
                    " but currently we only support"
                    " [Annotation, AudioAnnotation]."
                )

        def get_context_data(c_type, context):
            r"""Get context-specific data of a given context type and
                context.

            Args:
                c_type:
                    The granularity of the data context, which
                    could be any :class:`~forte.data.ontology.top.Annotation` type.
                context: context that
                    contains data to be extracted.

            Raises:
                NotImplementedError: raised when the given context type is
                    not implemented.

            Returns:
                str: context data.
            """
            if issubclass(c_type, Annotation):
                return self.text[context.begin : context.end]
            elif issubclass(c_type, AudioAnnotation):
                return self.audio[context.begin : context.end]
            else:
                raise NotImplementedError(
                    f"Context type is set to {context_type}"
                    "but currently we only support"
                    "[Annotation, AudioAnnotation]"
                )

        skipped = 0
        for context in get_annotation_list(context_type_):
            if context.tid not in valid_context_ids or not isinstance(
                context, context_type_
            ):
                continue
            if skipped < skip_k:
                skipped += 1
                continue
            data: Dict[str, Any] = {}
            data["context"] = get_context_data(context_type_, context)
            data["offset"] = context.begin

            for field in context_fields:
                data[field] = getattr(context, field)

            if annotation_types:
                for a_type, a_args in annotation_types.items():
                    if issubclass(a_type, context_type_):
                        continue
                    if a_type.__name__ in data:
                        raise KeyError(
                            f"Requesting two types of entries with the "
                            f"same class name {a_type.__name__} at the "
                            f"same time is not allowed"
                        )
                    data[
                        a_type.__name__
                    ] = self._generate_annotation_entry_data(
                        a_type, a_args, data, context
                    )

            if audio_annotation_types:
                for a_type, a_args in audio_annotation_types.items():
                    if a_type.__name__ in data:
                        raise KeyError(
                            f"Requesting two types of entries with the "
                            f"same class name {a_type.__name__} at the "
                            f"same time is not allowed"
                        )
                    data[
                        a_type.__name__
                    ] = self._generate_annotation_entry_data(
                        a_type, a_args, data, context
                    )

            if link_types:
                for l_type, l_args in link_types.items():
                    if l_type.__name__ in data:
                        raise KeyError(
                            f"Requesting two types of entries with the "
                            f"same class name {l_type.__name__} at the "
                            f"same time is not allowed"
                        )
                    data[l_type.__name__] = self._generate_link_entry_data(
                        l_type, l_args, data, context
                    )
            # TODO: Getting Group based on range is not done yet.
            if group_types:
                raise NotImplementedError(
                    "Querying groups based on ranges is "
                    "currently not supported."
                )
            if generics_types:
                raise NotImplementedError(
                    "Querying generic types based on ranges is "
                    "currently not supported."
                )
            yield data

    def _parse_request_args(self, a_type, a_args):
        # request which fields generated by which component
        components = None
        unit = None
        fields = set()
        if isinstance(a_args, dict):
            components = a_args.get("component")
            # pylint: disable=isinstance-second-argument-not-valid-type
            # TODO: until fix: https://github.com/PyCQA/pylint/issues/3507
            if components is not None and not isinstance(components, Iterable):
                raise TypeError(
                    "Invalid request format for 'components'. "
                    "The value of 'components' should be of an iterable type."
                )
            unit = a_args.get("unit")
            if unit is not None and not isinstance(unit, str):
                raise TypeError(
                    "Invalid request format for 'unit'. "
                    "The value of 'unit' should be a string."
                )
            a_args = a_args.get("fields", set())

        # pylint: disable=isinstance-second-argument-not-valid-type
        # TODO: disable until fix: https://github.com/PyCQA/pylint/issues/3507
        if isinstance(a_args, Iterable):
            fields = set(a_args)
        elif a_args is not None:
            raise TypeError(
                f"Invalid request format for '{a_type}'. "
                f"The request should be of an iterable type or a dict."
            )

        fields.add("tid")
        return components, unit, fields

    def _generate_annotation_entry_data(
        self,
        a_type: Union[Type[Annotation], Type[AudioAnnotation]],
        a_args: Union[Dict, Iterable],
        data: Dict,
        cont: Optional[Annotation],
    ) -> Dict:

        components, unit, fields = self._parse_request_args(a_type, a_args)

        a_dict: Dict[str, Any] = {}
        a_dict["span"] = []
        # For AudioAnnotation, since the data is single numpy array
        # we don't initialize an empty list for a_dict["audio"]
        if issubclass(a_type, Annotation):
            a_dict["text"] = []
        elif issubclass(a_type, AudioAnnotation):
            a_dict["audio"] = []

        for field in fields:
            a_dict[field] = []
        unit_begin = 0
        if unit is not None:
            if unit not in data:
                raise KeyError(
                    f"{unit} is missing in data. You need to "
                    f"request {unit} before {a_type}."
                )
            a_dict["unit_span"] = []

        cont_begin = cont.begin if cont else 0
        annotation: Union[Type[Annotation], Type[AudioAnnotation]]
        for annotation in self.get(a_type, cont, components):  # type: ignore
            # we provide span, text (and also tid) by default
            a_dict["span"].append((annotation.begin, annotation.end))

            if isinstance(annotation, Annotation):
                a_dict["text"].append(annotation.text)
            elif isinstance(annotation, AudioAnnotation):
                a_dict["audio"].append(annotation.audio)
            else:
                raise NotImplementedError(
                    f"Annotation is set to {annotation}"
                    "but currently we only support"
                    "instances of [Annotation, "
                    "AudioAnnotation] and their subclass."
                )
            for field in fields:
                if field in ("span", "text", "audio"):
                    continue
                if field == "context_span":
                    a_dict[field].append(
                        (
                            annotation.begin - cont_begin,
                            annotation.end - cont_begin,
                        )
                    )
                    continue

                a_dict[field].append(getattr(annotation, field))

            if unit is not None:
                while not self._index.in_span(
                    data[unit]["tid"][unit_begin],
                    annotation.span,
                ):
                    unit_begin += 1

                unit_span_begin = unit_begin
                unit_span_end = unit_span_begin + 1

                while self._index.in_span(
                    data[unit]["tid"][unit_span_end],
                    annotation.span,
                ):
                    unit_span_end += 1

                a_dict["unit_span"].append((unit_span_begin, unit_span_end))
        for key, value in a_dict.items():
            a_dict[key] = np.array(value)

        return a_dict

    def _generate_link_entry_data(
        self,
        a_type: Type[Link],
        a_args: Union[Dict, Iterable],
        data: Dict,
        cont: Optional[Annotation],
    ) -> Dict:

        components, unit, fields = self._parse_request_args(a_type, a_args)

        if unit is not None:
            raise ValueError(f"Link entries cannot be indexed by {unit}.")

        a_dict: Dict[str, Any] = {}
        for field in fields:
            a_dict[field] = []
        a_dict["parent"] = []
        a_dict["child"] = []

        link: Link
        for link in self.get(a_type, cont, components):
            parent_type = link.ParentType.__name__
            child_type = link.ChildType.__name__

            if parent_type not in data:
                raise KeyError(
                    f"The Parent entry of {a_type} is not requested."
                    f" You should also request {parent_type} with "
                    f"{a_type}"
                )
            if child_type not in data:
                raise KeyError(
                    f"The child entry of {a_type} is not requested."
                    f" You should also request {child_type} with "
                    f"{a_type}"
                )

            a_dict["parent"].append(
                np.where(data[parent_type]["tid"] == link.parent)[0][0]
            )
            a_dict["child"].append(
                np.where(data[child_type]["tid"] == link.child)[0][0]
            )

            for field in fields:
                if field in ("parent", "child"):
                    continue

                a_dict[field].append(getattr(link, field))

        for key, value in a_dict.items():
            a_dict[key] = np.array(value)
        return a_dict

    def build_coverage_for(
        self,
        context_type: Type[Union[Annotation, AudioAnnotation]],
        covered_type: Type[EntryType],
    ):
        """
        User can call this function to build coverage index for specific types.
        The index provide a in-memory mapping from entries of `context_type`
        to the entries "covered" by it.
        See :class:`forte.data.data_pack.DataIndex` for more details.

        Args:
            context_type: The context/covering type.
            covered_type: The entry to find under the context type.

        """
        if self._index.coverage_index(context_type, covered_type) is None:
            self._index.build_coverage_index(self, context_type, covered_type)

    def covers(
        self,
        context_entry: Union[Annotation, AudioAnnotation],
        covered_entry: EntryType,
    ) -> bool:
        """
        Check if the `covered_entry` is covered (in span) of the `context_type`.

        See :meth:`~forte.data.data_pack.DataIndex.in_span` and
        :meth:`~forte.data.data_pack.DataIndex.in_audio_span` for the definition
        of `in span`.

        Args:
            context_entry: The context entry.
            covered_entry: The entry to be checked on whether it is in span
              of the context entry.

        Returns (bool): True if in span.
        """
        return covered_entry.tid in self._index.get_covered(
            self, context_entry, covered_entry.__class__
        )

    def get(  # type: ignore
        self,
        entry_type: Union[str, Type[EntryType]],
        range_annotation: Optional[Union[Annotation, AudioAnnotation]] = None,
        components: Optional[Union[str, Iterable[str]]] = None,
        include_sub_type: bool = True,
    ) -> Iterable[EntryType]:
        r"""This function is used to get data from a data pack with various
        methods.

        Depending on the provided arguments, the function will perform several
        different filtering of the returned data.

        The ``entry_type`` is mandatory, where all the entries matching this
        type
        will be returned. The sub-types of the provided entry type will be
        also returned if ``include_sub_type`` is set to True (which is the
        default behavior).

        The ``range_annotation`` controls the search area of the sub-types. An
        entry `E` will be returned if
        :meth:`~forte.data.data_pack.DataIndex.in_span` or
        :meth:`~forte.data.data_pack.DataIndex.in_audio_span` returns True.
        If this function is called frequently
        with queries related to the ``range_annotation``, please consider to
        build
        the coverage index regarding the related entry types. User can call
        :meth:`build_coverage_for(context_type, covered_type)` in order to
        build
        a mapping between a pair of entry types and target entries that are
        covered in ranges specified by outer entries.

        The ``components`` list will filter the results by the `component` (i.e
        the creator of the entry). If ``components`` is provided, only the
        entries
        created by one of the ``components`` will be returned.

        Example:

            .. code-block:: python

                # Iterate through all the sentences in the pack.
                for sentence in input_pack.get(Sentence):
                    # Take all tokens from a sentence created by NLTKTokenizer.
                    token_entries = input_pack.get(
                        entry_type=Token,
                        range_annotation=sentence,
                        component='NLTKTokenizer')
                    ...

            In the above code snippet, we get entries of type ``Token`` within
            each ``sentence`` which were generated by ``NLTKTokenizer``. You
            can consider build coverage index between ``Token`` and
            ``Sentence``
            if this snippet is frequently used:

                .. code-block:: python

                    # Build coverage index between `Token` and `Sentence`
                    input_pack.build_coverage_for(
                        context_type=Sentence
                        covered_type=Token
                    )

            After building the index from the snippet above, you will be able
            to retrieve the tokens covered by sentence much faster.


        Args:
            entry_type: The type of entries requested.
            range_annotation: The
                range of entries requested. If `None`, will return valid
                entries in the range of whole data pack.
            components: The component (creator)
                generating the entries requested. If `None`, will return valid
                entries generated by any component.
            include_sub_type: whether to consider the sub types of
                the provided entry type. Default `True`.

        Yields:
            Each `Entry` found using this method.
        """
        entry_type_: Type[EntryType] = as_entry_type(entry_type)

        def require_annotations(entry_class=Annotation) -> bool:
            if issubclass(entry_type_, entry_class):
                return True
            if issubclass(entry_type_, Link):
                return issubclass(
                    entry_type_.ParentType, entry_class
                ) and issubclass(entry_type_.ChildType, entry_class)
            if issubclass(entry_type_, Group):
                return issubclass(entry_type_.MemberType, entry_class)
            return False

        # If we don't have any annotations but the items to check requires them,
        # then we simply yield from an empty list.
        if (
            len(self.annotations) == 0
            and isinstance(range_annotation, Annotation)
            and require_annotations(Annotation)
        ) or (
            len(self.audio_annotations) == 0
            and isinstance(range_annotation, AudioAnnotation)
            and require_annotations(AudioAnnotation)
        ):
            yield from []
            return

        # If the ``entry_type`` and `range_annotation` are for different types of
        # payload, then we yield from an empty list with a warning.
        if (
            require_annotations(Annotation)
            and isinstance(range_annotation, AudioAnnotation)
        ) or (
            require_annotations(AudioAnnotation)
            and isinstance(range_annotation, Annotation)
        ):
            logger.warning(
                "Incompatible combination of ``entry_type`` and "
                "`range_annotation` found in the input of `DataPack.get()`"
                " method. An empty iterator will be returned when inputs "
                "contain multi-media entries. Please double check the input "
                "arguments and make sure they are associated with the same type"
                " of payload (i.e., either text or audio)."
            )
            yield from []
            return

        try:
            for entry_data in self._data_store.get(
                type_name=get_full_module_name(entry_type_),
                include_sub_type=include_sub_type,
                range_annotation=range_annotation  # type: ignore
                and (range_annotation.begin, range_annotation.end),
            ):
                entry: EntryType = self.get_entry(tid=entry_data[TID_INDEX])
                # Filter by components
                if components is not None:
                    if not self.is_created_by(entry, components):
                        continue

                # Filter out incompatible audio span comparison for Links and Groups
                if (
                    issubclass(entry_type_, (Link, Group))
                    and isinstance(range_annotation, AudioAnnotation)
                    and not self._index.in_audio_span(
                        entry, range_annotation.span
                    )
                ):
                    continue

                yield entry
        except ValueError:
            # type_name does not exist in DataStore
            yield from []

    def update(self, datapack: "DataPack"):
        r"""Update the attributes and properties of the current DataPack with
        another DataPack.

        Args:
            datapack: A reference datapack to update
        """
        # TODO: Not recommended to directly update __dict__. Should find a
        #   better solution.
        self.__dict__.update(datapack.__dict__)

    def get_entry(self, tid: int) -> EntryType:
        r"""Look up the entry_index with ``tid``. Specific implementation
        depends on the actual class."""
        try:
            # Try to find entry in DataIndex
            entry: EntryType = self._index.get_entry(tid)
        except KeyError:
            # Find entry in DataStore
            entry = self._entry_converter.get_entry_object(tid, self)
        if entry is None:
            raise KeyError(
                f"There is no entry with tid '{tid}'' in this datapack"
            )
        return entry

    def get_entry_raw(self, tid: int) -> List:
        r"""Retrieve the raw entry data in list format from DataStore."""
        return self._data_store.get_entry(tid=tid)[0]

    def on_entry_creation(
        self, entry: Entry, component_name: Optional[str] = None
    ):
        """
        Call this when adding a new entry, will be called
        in :class:`~forte.data.ontology.core.Entry` when
        its `__init__` function is called.

        Here we override BasePack.on_entry_creation() to make sure each new
        entry is stored into ``DataStore`` on creation.

        Args:
            entry: The entry to be added.
            component_name: A name to record that the entry is created by
             this component.

        Returns:

        """
        c = component_name

        if c is None:
            # Use the auto-inferred control component.
            c = self.get_control_component()

        def entry_getter(cls: Entry, attr_name: str, field_type):
            """A getter function for dataclass fields of entry object.
            When the field contains ``tid``s, we will convert them to entry
            object on the fly.
            """
            data_store_ref = (
                cls.pack._data_store  # pylint: disable=protected-access
            )
            attr_val = data_store_ref.get_attribute(
                tid=cls.tid, attr_name=attr_name
            )
            if field_type in (FList, FDict):
                # Generate FList/FDict object on the fly
                return field_type(parent_entry=cls, data=attr_val)
            try:
                # TODO: Find a better solution to determine if a field is Entry
                if isinstance(attr_val, int):
                    # Convert tid to entry object on the fly
                    return cls.pack.get_entry(tid=attr_val)
            except KeyError:
                pass
            return attr_val

        def entry_setter(cls: Entry, value: Any, attr_name: str, field_type):
            """A setter function for dataclass fields of entry object.
            When the value contains entry objects, we will convert them into
            ``tid``s before storing to ``DataStore``.
            """
            attr_value: Any
            data_store_ref = (
                cls.pack._data_store  # pylint: disable=protected-access
            )
            if field_type is FList:
                attr_value = [
                    entry.tid if isinstance(entry, Entry) else entry
                    for entry in value
                ]
            elif field_type is FDict:
                attr_value = {
                    key: entry.tid if isinstance(entry, Entry) else entry
                    for key, entry in value.items()
                }
            elif isinstance(value, Entry):
                attr_value = value.tid
            else:
                attr_value = value
            data_store_ref.set_attribute(
                tid=cls.tid, attr_name=attr_name, attr_value=attr_value
            )

        # Save the input entry object in DataStore
        self._entry_converter.save_entry_object(entry=entry, pack=self)

        # Register property functions for all dataclass fields.
        for name, field in entry.__dataclass_fields__.items():
            field_type = get_origin(field.type)
            setattr(
                type(entry),
                name,
                property(
                    fget=partial(
                        entry_getter, attr_name=name, field_type=field_type
                    ),
                    fset=partial(
                        entry_setter, attr_name=name, field_type=field_type
                    ),
                ),
            )

        # Record that this entry hasn't been added to the index yet.
        self._pending_entries[entry.tid] = entry.tid, c

    def __del__(self):
        super().__del__()
        # Remove all the remaining tids in _pending_entries.
        tids: List = list(self._pending_entries.keys())
        for tid in tids:
            self._pending_entries.pop(tid)
            self._data_store.delete_entry(tid=tid)


class DataIndex(BaseIndex):
    r"""A set of indexes used in :class:`~forte.data.data_pack.DataPack`, note that this class is
    used by the `DataPack` internally.

    #. :attr:`entry_index`, the index from each ``tid`` to the corresponding entry
    #. :attr:`type_index`, the index from each type to the entries of
       that type
    #. :attr:`component_index`, the index from each component to the
       entries generated by that component
    #. :attr:`link_index`, the index from child
       (:attr:`link_index["child_index"]`)and parent
       (:attr:`link_index["parent_index"]`) nodes to links
    #. :attr:`group_index`, the index from group members to groups.
    #. :attr:`_coverage_index`, the index that maps from an annotation to
       the entries it covers. :attr:`_coverage_index` is a dict of dict, where
       the key is a tuple of the outer entry type and the inner entry type.
       The outer entry type should be an annotation type. The value is a dict,
       where the key is the ``tid`` of the outer entry, and the value is a set of
       ``tid`` that are covered by the outer entry. We say an Annotation A covers
       an entry E if one of the following condition is met:
       1. E is of Annotation type, and that E.begin >= A.begin, E.end <= E.end
       2. E is of Link type, and both E's parent and child node are Annotation
       that are covered by A.

    """

    def __init__(self):
        super().__init__()
        self._coverage_index: Dict[
            Tuple[Type[Union[Annotation, AudioAnnotation]], Type[EntryType]],
            Dict[int, Set[int]],
        ] = {}
        self._coverage_index_valid = True

    def remove_entry(self, entry: EntryType):
        super().remove_entry(entry)
        self.deactivate_coverage_index()

    @property
    def coverage_index_is_valid(self):
        return self._coverage_index_valid

    def activate_coverage_index(self):
        self._coverage_index_valid = True

    def deactivate_coverage_index(self):
        self._coverage_index_valid = False

    def coverage_index(
        self,
        outer_type: Type[Union[Annotation, AudioAnnotation]],
        inner_type: Type[EntryType],
    ) -> Optional[Dict[int, Set[int]]]:
        r"""Get the coverage index from ``outer_type`` to ``inner_type``.

        Args:
            outer_type: an annotation or `AudioAnnotation` type.
            inner_type: an entry type.

        Returns:
            If the coverage index does not exist, return `None`. Otherwise,
            return a dict.
        """
        if not self.coverage_index_is_valid:
            return None
        return self._coverage_index.get((outer_type, inner_type))

    def get_covered(
        self,
        data_pack: DataPack,
        context_annotation: Union[Annotation, AudioAnnotation],
        inner_type: Type[EntryType],
    ) -> Set[int]:
        """
        Get the entries covered by a certain context annotation

        Args:
            data_pack: The data pack to search for.
            context_annotation: The context annotation to search in.
            inner_type: The inner type to be searched for.

        Returns:
            Entry ID of type `inner_type` that is covered by
            `context_annotation`.
        """
        context_type = context_annotation.__class__
        if self.coverage_index(context_type, inner_type) is None:
            self.build_coverage_index(data_pack, context_type, inner_type)
        assert self._coverage_index is not None
        return self._coverage_index.get((context_type, inner_type), {}).get(
            context_annotation.tid, set()
        )

    def build_coverage_index(
        self,
        data_pack: DataPack,
        outer_type: Type[Union[Annotation, AudioAnnotation]],
        inner_type: Type[EntryType],
    ):
        r"""Build the coverage index from ``outer_type`` to ``inner_type``.

        Args:
            data_pack: The data pack to build coverage for.
            outer_type: an annotation or `AudioAnnotation` type.
            inner_type: an entry type, can be Annotation, Link, Group,
                `AudioAnnotation`.
        """
        if not issubclass(
            inner_type, (Annotation, Link, Group, AudioAnnotation)
        ):
            raise ValueError(f"Do not support coverage index for {inner_type}.")

        if not self.coverage_index_is_valid:
            self._coverage_index = {}

        # prevent the index from being used during construction
        self.deactivate_coverage_index()

        # TODO: tests and documentations for the edge cases are missing. i.e. we
        #  are not clear about what would happen if the covered annotation
        #  is the same as the covering annotation, or if their spans are the
        #  same.
        self._coverage_index[(outer_type, inner_type)] = {}
        for range_annotation in data_pack.get_entries_of(outer_type):
            if isinstance(range_annotation, (Annotation, AudioAnnotation)):
                entries = data_pack.get(inner_type, range_annotation)
                entry_ids = {e.tid for e in entries}
                self._coverage_index[(outer_type, inner_type)][
                    range_annotation.tid
                ] = entry_ids

        self.activate_coverage_index()

    def have_overlap(
        self,
        entry1: Union[Annotation, int, AudioAnnotation],
        entry2: Union[Annotation, int, AudioAnnotation],
    ) -> bool:
        r"""Check whether the two annotations have overlap in span.

        Args:
            entry1: An
                :class:`Annotation` or :class:`AudioAnnotation` object to be
                checked, or the ``tid`` of the Annotation.
            entry2: Another
                :class:`Annotation` or :class:`AudioAnnotation` object to be
                checked, or the ``tid`` of the Annotation.
        """
        entry1_: Union[Annotation, AudioAnnotation] = (
            self._entry_index[entry1]
            if isinstance(entry1, (int, np.integer))
            else entry1
        )
        entry2_: Union[Annotation, AudioAnnotation] = (
            self._entry_index[entry2]
            if isinstance(entry2, (int, np.integer))
            else entry2
        )

        if not isinstance(entry1_, (Annotation, AudioAnnotation)):
            raise TypeError(
                f"'entry1' should be an instance of Annotation or `AudioAnnotation`,"
                f" but get {type(entry1)}"
            )

        if not isinstance(entry2_, (Annotation, AudioAnnotation)):
            raise TypeError(
                f"'entry2' should be an instance of Annotation or `AudioAnnotation`,"
                f" but get {type(entry2)}"
            )

        if (
            isinstance(entry1_, Annotation)
            and isinstance(entry2_, AudioAnnotation)
        ) or (
            isinstance(entry1_, AudioAnnotation)
            and isinstance(entry2_, Annotation)
        ):
            raise TypeError(
                "'entry1' and 'entry2' should be the same type of entry, "
                f"but get type(entry1)={type(entry1_)}, "
                f"typr(entry2)={type(entry2_)}"
            )

        return not (
            entry1_.begin >= entry2_.end or entry1_.end <= entry2_.begin
        )

    def in_span(self, inner_entry: Union[int, Entry], span: Span) -> bool:
        r"""Check whether the ``inner entry`` is within the given ``span``. The
        criterion are as followed:

        Annotation entries: they are considered in a span if the
        begin is not smaller than `span.begin` and the end is not larger than
        `span.end`.

        Link entries: if the parent and child of the links are both
        `Annotation` type, this link will be considered in span if both parent
        and child are :meth:`~forte.data.data_pack.DataIndex.in_span` of the
        provided `span`. If either the parent and
        the child is not of type `Annotation`, this function will always return
        `False`.

        Group entries: if the child type of the group is `Annotation` type,
        then the group will be considered in span if all the elements are
        :meth:`~forte.data.data_pack.DataIndex.in_span` of the provided `span`.
        If the child type is not `Annotation`
        type, this function will always return `False`.

        Other entries (i.e Generics and `AudioAnnotation`): they will not be
        considered :meth:`~forte.data.data_pack.DataIndex.in_span` of any
        spans. The function will always return
        `False`.

        Args:
            inner_entry: The inner entry object to be checked
             whether it is within ``span``. The argument can be the entry id
             or the entry object itself.
            span: A :class:`~forte.data.span.Span` object to be checked. We will check
                whether the ``inner_entry`` is within this span.

        Returns:
            True if the `inner_entry` is considered to be in span of the
            provided span.
        """
        # The reason of this check is that the get_data method will use numpy
        # integers. This might create problems when other unexpected integers
        # are used.
        if isinstance(inner_entry, (int, np.integer)):
            inner_entry = self._entry_index[inner_entry]

        inner_begin = -1
        inner_end = -1

        if isinstance(inner_entry, Annotation):
            inner_begin = inner_entry.begin
            inner_end = inner_entry.end
        elif isinstance(inner_entry, Link):
            if not issubclass(inner_entry.ParentType, Annotation):
                return False

            if not issubclass(inner_entry.ChildType, Annotation):
                return False

            child = inner_entry.get_child()
            parent = inner_entry.get_parent()

            if not isinstance(child, Annotation) or not isinstance(
                parent, Annotation
            ):
                # Cannot check in_span for non-annotations.
                return False

            child_: Annotation = child
            parent_: Annotation = parent

            inner_begin = min(child_.begin, parent_.begin)
            inner_end = max(child_.end, parent_.end)
        elif isinstance(inner_entry, Group):
            if not issubclass(inner_entry.MemberType, Annotation):
                return False

            for mem in inner_entry.get_members():
                mem_: Annotation = mem  # type: ignore
                if inner_begin == -1:
                    inner_begin = mem_.begin
                inner_begin = min(inner_begin, mem_.begin)
                inner_end = max(inner_end, mem_.end)
        else:
            # Generics, AudioAnnotation, or other user defined types will not
            # be check here.
            return False
        return inner_begin >= span.begin and inner_end <= span.end

    def in_audio_span(self, inner_entry: Union[int, Entry], span: Span) -> bool:
        r"""Check whether the ``inner entry`` is within the given audio span.
        This method is identical to
        :meth::meth:`~forte.data.data_pack.DataIndex.in_span` except that it
        operates on
        the audio payload of datapack. The criterion are as followed:

        `AudioAnnotation` entries: they are considered in a span if the
        begin is not smaller than `span.begin` and the end is not larger than
        `span.end`.

        Link entries: if the parent and child of the links are both
        `AudioAnnotation` type, this link will be considered in span if both
        parent and child are :meth:`~forte.data.data_pack.DataIndex.in_span` of
        the provided `span`. If either the
        parent and the child is not of type `AudioAnnotation`, this function
        will always return `False`.

        Group entries: if the child type of the group is `AudioAnnotation`
        type,
        then the group will be considered in span if all the elements are
        :meth:`~forte.data.data_pack.DataIndex.in_span` of the provided `span`.
        If the child type is not
        `AudioAnnotation` type, this function will always return `False`.

        Other entries (i.e Generics and Annotation): they will not be
        considered
        :meth:`~forte.data.data_pack.DataIndex.in_span` of any spans. The
        function will always return `False`.

        Args:
            inner_entry: The inner entry object to be checked
                whether it is within ``span``. The argument can be the entry id
                or the entry object itself.
            span: A :class:`~forte.data.span.Span` object to be checked.
                We will check whether the ``inner_entry`` is within this span.

        Returns:
            True if the `inner_entry` is considered to be in span of the
            provided span.
        """
        # The reason of this check is that the get_data method will use numpy
        # integers. This might create problems when other unexpected integers
        # are used.
        if isinstance(inner_entry, (int, np.integer)):
            inner_entry = self._entry_index[inner_entry]

        inner_begin = -1
        inner_end = -1

        if isinstance(inner_entry, AudioAnnotation):
            inner_begin = inner_entry.begin
            inner_end = inner_entry.end
        elif isinstance(inner_entry, Link):
            if not (
                issubclass(inner_entry.ParentType, AudioAnnotation)
                and issubclass(inner_entry.ChildType, AudioAnnotation)
            ):
                return False

            child = inner_entry.get_child()
            parent = inner_entry.get_parent()

            if not isinstance(child, AudioAnnotation) or not isinstance(
                parent, AudioAnnotation
            ):
                # Cannot check in_span for non-AudioAnnotation.
                return False

            child_: AudioAnnotation = child
            parent_: AudioAnnotation = parent

            inner_begin = min(child_.begin, parent_.begin)
            inner_end = max(child_.end, parent_.end)
        elif isinstance(inner_entry, Group):
            if not issubclass(inner_entry.MemberType, AudioAnnotation):
                return False

            for mem in inner_entry.get_members():
                mem_: AudioAnnotation = mem  # type: ignore
                if inner_begin == -1:
                    inner_begin = mem_.begin
                inner_begin = min(inner_begin, mem_.begin)
                inner_end = max(inner_end, mem_.end)
        else:
            # Generics, Annotation, or other user defined types will not be
            # check here.
            return False
        return inner_begin >= span.begin and inner_end <= span.end


class EntryConverter:
    r"""
    Facilitate the conversion between entry data in list format from
    ``DataStore`` and entry class object.
    """

    def __init__(self) -> None:
        # Mapping from entry's tid to the entry objects for caching
        self._entry_dict: Dict[int, Entry] = {}

    def save_entry_object(
        self, entry: Entry, pack: DataPack, allow_duplicate: bool = True
    ):
        """
        Save an existing entry object into DataStore.
        """
        # Check if the entry is already stored
        data_store_ref = pack._data_store  # pylint: disable=protected-access
        try:
            data_store_ref.get_entry(tid=entry.tid)
            logger.info(
                "The entry with tid=%d is already saved into DataStore",
                entry.tid,
            )
            return
        except KeyError:
            # The entry is not found in DataStore
            pass

        # Create a new registry in DataStore based on entry's type
        if isinstance(entry, Annotation):
            data_store_ref.add_annotation_raw(
                type_name=entry.entry_type(),
                begin=entry.begin,
                end=entry.end,
                tid=entry.tid,
                allow_duplicate=allow_duplicate,
            )
        elif isinstance(entry, Link):
            data_store_ref.add_link_raw(
                type_name=entry.entry_type(),
                parent_tid=entry.parent,
                child_tid=entry.child,
                tid=entry.tid,
            )
        elif isinstance(entry, Group):
            data_store_ref.add_group_raw(
                type_name=entry.entry_type(),
                member_type=get_full_module_name(entry.MemberType),
                tid=entry.tid,
            )
        elif isinstance(entry, Generics):
            data_store_ref.add_generics_raw(
                type_name=entry.entry_type(),
                tid=entry.tid,
            )
        elif isinstance(entry, AudioAnnotation):
            data_store_ref.add_audio_annotation_raw(
                type_name=entry.entry_type(),
                begin=entry.begin,
                end=entry.end,
                tid=entry.tid,
                allow_duplicate=allow_duplicate,
            )
        elif isinstance(entry, ImageAnnotation):
            data_store_ref.add_image_annotation_raw(
                type_name=entry.entry_type(),
                image_payload_idx=entry.image_payload_idx,
                tid=entry.tid,
                allow_duplicate=allow_duplicate,
            )
        elif isinstance(entry, Grids):
            data_store_ref.add_grid_raw(
                type_name=entry.entry_type(),
                image_payload_idx=entry.image_payload_idx,
                tid=entry.tid,
                allow_duplicate=allow_duplicate,
            )
        else:
            raise ValueError(
                f"Invalid entry type {type(entry)}. A valid entry "
                f"should be an instance of Annotation, Link, Group, Generics "
                "or AudioAnnotation."
            )

        # Store all the dataclass attributes to DataStore
        for attribute in entry.__dataclass_fields__:
            value = getattr(entry, attribute, None)
            if not value:
                continue
            if isinstance(value, Entry):
                value = value.tid
            elif isinstance(value, FDict):
                value = {key: val.tid for key, val in value.items()}
            elif isinstance(value, FList):
                value = [val.tid for val in value]
            data_store_ref.set_attribute(
                tid=entry.tid, attr_name=attribute, attr_value=value
            )

        # Cache the stored entry and its tid
        self._entry_dict[entry.tid] = entry

    def get_entry_object(self, tid: int, pack: DataPack) -> EntryType:
        """
        Convert a tid to its corresponding entry object.
        """

        # Check if the tid is cached
        if tid in self._entry_dict:
            return self._entry_dict[tid]  # type: ignore

        data_store_ref = pack._data_store  # pylint: disable=protected-access
        entry_data, entry_type = data_store_ref.get_entry(tid=tid)
        entry_class = get_class(entry_type)
        entry: Entry
        # Here the entry arguments are optional (begin, end, parent, ...) and
        # the value can be arbitrary since they will all be routed to DataStore.
        if issubclass(entry_class, (Annotation, AudioAnnotation)):
            entry = entry_class(pack=pack, begin=0, end=0)
        elif issubclass(entry_class, (Link, Group, Generics)):
            entry = entry_class(pack=pack)
        else:
            raise ValueError(
                f"Invalid entry type {type(entry_class)}. A valid entry "
                f"should be an instance of Annotation, Link, Group, Generics "
                "or AudioAnnotation."
            )

        # TODO: Remove the new tid and direct the entry object to the correct
        # tid. The implementation here is a little bit hacky. Will need a stable
        # solution in future.
        # pylint: disable=protected-access
        if entry.tid in self._entry_dict:
            self._entry_dict.pop(entry.tid)
        if entry.tid in pack._pending_entries:
            pack._pending_entries.pop(entry.tid)
        data_store_ref.delete_entry(tid=entry.tid)
        entry._tid = entry_data[TID_INDEX]

        self._entry_dict[tid] = entry
        return entry  # type: ignore
