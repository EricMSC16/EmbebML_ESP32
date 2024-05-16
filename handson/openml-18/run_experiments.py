
import os
import glob
import uuid

from sklearn.metrics import roc_auc_score, make_scorer
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import RobustScaler, FunctionTransformer
from sklearn.ensemble import RandomForestClassifier
from sklearn.model_selection import cross_validate
from sklearn.cluster import KMeans

import pandas
import numpy
import structlog

log = structlog.get_logger()

def linear_quantize(img, target_min, target_max, dtype):
    imin = img.min()
    imax = img.max()

    a = (target_max - target_min) / (imax - imin)
    b = target_max - a * imax
    new_img = (a * img + b).astype(dtype, casting='unsafe')
    return new_img


def get_tree_estimators(estimator):
    """
    Get the DecisionTree instances from ensembles or single-tree models
    """
    if hasattr(estimator, 'estimators_'):
        trees = [ e for e in estimator.estimators_]
    else:
        trees = [ estimator ]
    return trees

def tree_nodes(model, a=None, b=None):
    """
    Number of nodes total
    """
    model = model.named_steps['customrandomforestclassifier']

    trees = get_tree_estimators(model)
    nodes = [ len(e.tree_.children_left) for e in trees ]
    return numpy.sum(nodes)

def tree_leaves(model, a=None, b=None):
    """
    Average depth of model
    """
    model = model.named_steps['customrandomforestclassifier']

    trees = get_tree_estimators(model)
    leaves = [ numpy.count_nonzero((e.tree_.children_left == -1) & (e.tree_.children_right == -1)) for e in trees ]
    return numpy.sum(leaves)

def leaf_size(model, a=None, b=None):
    """
    Average depth of model
    """
    model = model.named_steps['customrandomforestclassifier']

    trees = get_tree_estimators(model)
    sizes = [ e.tree_.value[(e.tree_.children_left == -1) & (e.tree_.children_right == -1)].shape[-1] for e in trees ]
    return numpy.median(sizes)

class CustomRandomForestClassifier(RandomForestClassifier):

    def __init__(self, clusters=100, **kwargs):
        # FIXME: parameters passing does not work
        print('INIT', kwargs)
        super().__init__(**kwargs)

        self.clusters = clusters

    def get_params(self, deep=True):
        return super().get_params()    

    def set_params(self, **parameters):
        print("SET", **parameters)
        return super().set_params(**parameters)

    def fit(self, X, y):
        ret = super().fit(X, y)

        # Find leaves
        ll = []
        for e in self.estimators_:
            is_leaf = (e.tree_.children_left == -1) & (e.tree_.children_right == -1)
            l = e.tree_.value[is_leaf]
            ll.append(l)

        leaves = numpy.concatenate(ll)
        assert leaves.shape[1] == 1, 'only single output supported'
        leaves = numpy.squeeze(leaves)

        # Cluster the leaves
        cluster = KMeans(n_clusters=self.clusters, tol=1e-4, max_iter=100)
        cluster.fit(leaves)

        # Replace by closest centroid
        for e in self.estimators_:

            is_leaf = (e.tree_.children_left == -1) & (e.tree_.children_right == -1)
            values = e.tree_.value
            assert values.shape[1] == 1
            c_idx = cluster.predict(numpy.squeeze(values))
            centroids = cluster.cluster_centers_[c_idx]
            # XXX: is this correct ??
            v = numpy.where(numpy.expand_dims(is_leaf, -1), centroids, numpy.squeeze(values))
            v = numpy.reshape(v, values.shape)

            for i in range(len(e.tree_.value)):
                e.tree_.value[i] = v[i]


        return ret        



def run_dataset(pipeline, dataset_path,
    n_jobs = 4,
    repetitions = 1,
    scoring = 'roc_auc_ovo_weighted',
    ):

    data = pandas.read_parquet(dataset_path)
    target_column = '__target'
    feature_columns = list(set(data.columns) - set([target_column]))
    Y = data[target_column]
    X = data[feature_columns]

    scoring = {
        'nodes': tree_nodes,
        'leaves': tree_leaves,
        'leasize': leaf_size,
        'roc_auc': 'roc_auc_ovo_weighted',
    }

    dfs = []

    for repetition in range(repetitions):
        out = cross_validate(pipeline, X, Y, cv=10, n_jobs=n_jobs, scoring=scoring)
        df = pandas.DataFrame.from_records(out)        
        df['repetition'] = repetition
        dfs.append(df)
    
    out = pandas.concat(dfs)
    return out

def ensure_dir(p):
    if not os.path.exists(p):
        os.makedirs(p)

def run_datasets(pipeline, out_dir, run_id, kvs={}, dataset_dir=None, **kwargs):

    if dataset_dir is None:
        dataset_dir = 'data/datasets'

    for f in glob.glob('*.parquet', root_dir=dataset_dir):
        dataset_id = os.path.splitext(f)[0]
        dataset_path = os.path.join(dataset_dir, f)

        res = run_dataset(pipeline, dataset_path, **kwargs)
        res['dataset'] = str(dataset_id)
        for k, v in kvs.items():
            res[k] = v
        res['run'] = run_id

        #o = os.path.join(out_dir, f'dataset={dataset_id}')
        #ensure_dir(o)
        res.to_parquet(os.path.join(out_dir, f'r_{run_id}_ds_{dataset_id}.part'))

        print(res)

        log.info('dataset-run-end', dataset=dataset_id)

def main():

    # optimization steps. To be compared in experiments / ablated
    # reduce number of features. To <255
    # quantize features to int16
    # reduce number of trees
    # try replace nodes with k-means quantized versions

    quantizer = FunctionTransformer(linear_quantize,
        kw_args=dict(target_min=0, target_max=255, dtype=numpy.uint8))

    p = make_pipeline(
        RobustScaler(),
        #quantizer,
        CustomRandomForestClassifier(n_estimators=2, max_depth=5),
    )

    run_id = uuid.uuid4().hex.upper()[0:6]

    out = run_datasets(p, kvs=dict(experiment='rf10'), out_dir='out.parquet', run_id=run_id)



if __name__ == '__main__':
    main()
