import json
import logging
from datetime import datetime
from typing import (
    Callable,
    Iterable,
    List,
    NamedTuple,
    Sequence,
    Tuple,
    TypeVar,
    Union,
)

from tqdm import tqdm

from ctparse.ctparse import ctparse_gen
from ctparse.scorer import DummyScorer, Scorer
from ctparse.types import Artifact, Duration, Interval, Time

logger = logging.getLogger(__name__)

# A triplet of text, reference timestamp and correct parse.
# It can be used as raw data to build datasets for ctparse.
TimeParseEntry = NamedTuple(
    "TimeParseEntry",
    [("text", str), ("ts", datetime), ("gold", Artifact)],
)

T = TypeVar("T")


def make_partial_rule_dataset(
    entries: Sequence[TimeParseEntry],
    scorer: Scorer,
    timeout: Union[float, int],
    max_stack_depth: int,
    relative_match_len: float = 1.0,
    progress: bool = False,
) -> Iterable[Tuple[List[str], bool]]:
    """Build a data set from an iterable of TimeParseEntry.

    The text is run through ctparse and all parses (within the specified timeout,
    max_stack_depth and scorer) are obtained. Each parse contains a sequence
    of rules (see ``CTParse.rules``) used to produce that parse.

    A dataset is generated by taking every possible partial rule and assigning to it
    a boolean indicating if that partial sequence did lead to a successful parse.

    If `progress` is ``True``, display a progress bar.

    Example:

    rule sequence: [r1, r2, r3]
    parse_is_correct: True

    [r1] -> True
    [r1, r2] -> True
    [r1, r2, r3] -> True
    """
    # If we look at the signature for a scorer, the score is obtained from:
    # (text, reference_time, partial_parse) and optionally a production for a
    # partial parse.
    # Clearly, if we were to make a general scorer for the dataset, we would need
    # all of these features. It is possible to achieve that by tracking the list of
    # partial parses that led to a correct parse. Unfortunately we don't have the
    # full history with the current implementation, however we can obtain a dataset
    # of (text, reference_time, rule_ids) quite easily, because the rule is a linear
    # list.

    if progress:
        entries_it = _progress_bar(
            entries,
            total=len(entries),
            status_text=lambda entry: "  {: <70}".format(entry.text),
        )
    else:
        entries_it = entries

    for entry in entries_it:
        for parse in ctparse_gen(
            entry.text,
            entry.ts,
            relative_match_len=relative_match_len,
            timeout=timeout,
            max_stack_depth=max_stack_depth,
            scorer=scorer,
            latent_time=False,
        ):
            # TODO: we should make sure ctparse_gen never returns None. If there is no
            # result it should return an empty list
            if parse is None:
                continue

            y = parse.resolution == entry.gold
            # Build data set, one sample for each applied rule in
            # the sequence of rules applied in this production
            # *after* the matched regular expressions
            for i in range(1, len(parse.production) + 1):
                X = [str(p) for p in parse.production[:i]]
                yield X, y


def _progress_bar(
    it: Iterable[T], total: int, status_text: Callable[[T], str]
) -> Iterable[T]:
    # Progress bar that can update text
    pbar = tqdm(it, total=total)
    for val in pbar:
        pbar.set_description(status_text(val))
        yield val


def load_timeparse_corpus(fname: str) -> Sequence[TimeParseEntry]:
    """Load a corpus from disk.

    For more information about the format of the time parse corpus,
    refer to the documentation.
    """
    with open(fname, "r", encoding="utf-8") as fd:
        entries = json.load(fd)

    return [
        TimeParseEntry(
            text=e["text"],
            ts=datetime.strptime(e["ref_time"], "%Y-%m-%dT%H:%M:%S"),
            gold=parse_nb_string(e["gold_parse"]),
        )
        for e in entries
    ]


def parse_nb_string(gold_parse: str) -> Union[Time, Interval, Duration]:
    """Parse a Time, Interval or Duration from their no-bound string representation.

    The no-bound string representations are generated from ``Artifact.nb_str``.
    """
    if gold_parse.startswith("Time"):
        return Time.from_str(gold_parse[7:-1])
    if gold_parse.startswith("Interval"):
        return Interval.from_str(gold_parse[11:-1])
    if gold_parse.startswith("Duration"):
        return Duration.from_str(gold_parse[11:-1])
    else:
        raise ValueError("'{}' has an invalid format".format(gold_parse))


def _run_corpus_one_test(
    target: str, ts_str: str, tests: List[str], max_stack_depth: int = 0
) -> Tuple[List[List[str]], List[bool], int, int, int, int, int, bool]:
    ts = datetime.strptime(ts_str, "%Y-%m-%dT%H:%M")
    all_tests_pass = True
    Xs = []
    ys = []
    pos_parses = neg_parses = pos_first_parses = pos_best_scored = total_tests = 0

    at_least_one_failed = False
    for test in tests:
        one_prod_passes = False
        first_prod = True
        y_score = []
        for parse in ctparse_gen(
            test,
            ts,
            relative_match_len=1.0,
            timeout=0,
            max_stack_depth=max_stack_depth,
            scorer=DummyScorer(),
            latent_time=False,
        ):
            assert parse is not None

            y = parse.resolution.nb_str() == target
            # Build data set, one sample for each applied rule in
            # the sequence of rules applied in this production
            # *after* the matched regular expressions
            for i in range(1, len(parse.production) + 1):
                Xs.append([str(p) for p in parse.production[:i]])
                ys.append(y)

            one_prod_passes |= y
            pos_parses += int(y)
            neg_parses += int(not y)
            pos_first_parses += int(y and first_prod)
            first_prod = False
            y_score.append((parse.score, y))
        if not one_prod_passes:
            logger.warning(
                'failure: target "{}" never produced in "{}"'.format(target, test)
            )
        pos_best_scored += int(max(y_score, key=lambda x: x[0])[1])
        total_tests += len(tests)
        all_tests_pass &= one_prod_passes
    if not all_tests_pass:
        logger.warning('failure: "{}" not always produced'.format(target))
        at_least_one_failed = True
    return (
        Xs,
        ys,
        total_tests,
        pos_parses,
        neg_parses,
        pos_first_parses,
        pos_best_scored,
        at_least_one_failed,
    )


def run_corpus(
    corpus: Sequence[Tuple[str, str, Sequence[str]]]
) -> Tuple[List[List[str]], List[bool]]:
    """Load the corpus (currently hard coded), run it through ctparse with
    no timeout and no limit on the stack depth.

    The corpus passes if ctparse generates the desired solution for
    each test at least once. Otherwise it fails.

    While testing this, a labeled data set (X, y) is generated based
    on *all* productions. Given a final production p, based on initial
    regular expression matches r_0, ..., r_n, which are then
    subsequently transformed using production rules p_0, ..., p_m,
    will result in the samples

    [r_0, ..., r_n, p_0, 'step_0']
    [r_0, ..., r_n, p_0, p_1, 'step_1']
    ...
    [r_0, ..., r_n, p_0, ..., p_m, 'step_m']

    All samples from one production are given the same label which indicates if
    the production was correct.

    To build a similar datasets without the strict checking, use
    `make_partial_rule_dataset`
    """
    at_least_one_failed = False
    # pos_parses: number of parses that are correct
    # neg_parses: number of parses that are wrong
    # pos_first_parses: number of first parses generated that are correct
    # pos_best_scored: number of correct parses that have the best score
    pos_parses = neg_parses = pos_first_parses = pos_best_scored = 0
    total_tests = 0
    Xs = []
    ys = []
    for target, ts, tests in tqdm(corpus):
        (
            Xs_,
            ys_,
            total_tests_,
            pos_parses_,
            neg_parses_,
            pos_first_parses_,
            pos_best_scored_,
            at_least_one_failed_,
        ) = _run_corpus_one_test(target, ts, tests)
        Xs.extend(Xs_)
        ys.extend(ys_)
        total_tests += total_tests_
        pos_parses += pos_parses_
        neg_parses += neg_parses_
        pos_first_parses += pos_first_parses_
        pos_best_scored += pos_best_scored_
        at_least_one_failed = at_least_one_failed or at_least_one_failed_
    logger.info(
        "run {} tests on {} targets with a total of "
        "{} positive and {} negative parses (={})".format(
            total_tests, len(corpus), pos_parses, neg_parses, pos_parses + neg_parses
        )
    )
    logger.info(
        "share of correct parses in all parses: {:.2%}".format(
            pos_parses / (pos_parses + neg_parses)
        )
    )
    logger.info(
        "share of correct parses being produced first: {:.2%}".format(
            pos_first_parses / (pos_parses + neg_parses)
        )
    )
    logger.info(
        "share of correct parses being scored highest: {:.2%}".format(
            pos_best_scored / total_tests
        )
    )
    if at_least_one_failed:
        raise Exception("ctparse corpus has errors")
    return Xs, ys
