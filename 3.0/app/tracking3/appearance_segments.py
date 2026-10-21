"""Experimental appearance change proposals, independent of identity labels.

These partitions cover recorded detections only; a proposed boundary never
confirms identity or visibility between samples. No production default changes.
"""
from collections import deque
import math

import numpy as np


def propose(vectors, *, threshold, window=5):
    """Split after two dissimilar-to-reference but mutually coherent samples.

    An isolated anomaly stays in the original segment and does not update its
    reference. Confirmation is retrospective to the first of the two samples.
    Missing time/visibility is deliberately outside this appearance-only rule.
    """
    if isinstance(threshold,bool) or not math.isfinite(threshold) or not 0<threshold<1:
        raise ValueError('外观阈值须位于0与1之间')
    if type(window) is not int or window<1:
        raise ValueError('外观参考窗口须为正整数')
    if not len(vectors):return dict(segments=[],cuts=[],identity_certified=False)
    values=np.asarray(vectors,dtype=np.float64)
    if values.ndim!=2 or not values.shape[1] or not np.isfinite(values).all():
        raise ValueError('外观向量须为同维度有限数组')
    norms=np.linalg.norm(values,axis=1)
    if not np.isfinite(norms).all() or (norms<=0).any():raise ValueError('外观向量不能为零')
    values=values/norms[:,None]
    history=deque([values[0]],maxlen=window)
    current=[0];pending=[];segments=[];cuts=[]
    def score(a,b):return float(np.clip(np.dot(a,b),-1,1))
    for index in range(1,len(values)):
        mean=np.mean(history,axis=0);norm=np.linalg.norm(mean)
        reference=mean/norm if norm>1e-12 else history[-1]
        similarity=score(values[index],reference)
        if similarity>=threshold:
            current.extend(pending);pending=[]
            current.append(index);history.append(values[index])
            continue
        pending.append(index)
        if len(pending)==2:
            coherence=score(values[pending[0]],values[pending[1]])
            if coherence>=threshold:
                segments.append(current)
                cuts.append(dict(first_new_index=pending[0],confirmed_at_index=index,
                    similarity_to_previous_reference=similarity,new_pair_similarity=coherence))
                current=pending;pending=[]
                history=deque((values[i] for i in current),maxlen=window)
            else:
                # Do not place a third unrelated point in a coherent new segment.
                current.append(pending.pop(0))
    current.extend(pending);segments.append(current)
    return dict(segments=segments,cuts=cuts,identity_certified=False)
