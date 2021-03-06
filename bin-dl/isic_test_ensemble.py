import os
import argparse
import logging

import SimpleITK as sitk
import numpy as np
import torch
import pymia.data.assembler as assembler
import torch.nn.functional as F

import common.evalutation.eval as ev
import common.trainloop.data as data
import common.trainloop.steps as step
import common.trainloop.context as ctx
import common.trainloop.hooks as hooks
import common.trainloop.loops as loop
import common.utils.messages as msg
import common.utils.threadhelper as thread
import common.model.management as mgt
import rechun.dl.customdatasets as isic
import rechun.dl.customsteps as customstep
import rechun.directories as dirs


def main(config_file):

    if config_file is None:
        config_file = os.path.join(dirs.CONFIG_DIR, 'test_isic_ensemble.yaml')

    context = ctx.TorchTestContext('cuda')
    context.load_from_config(config_file)

    build_test = data.BuildData(
        build_dataset=isic.BuildIsicDataset(),
    )

    if not hasattr(context.config.others, 'model_dir') or not hasattr(context.config.others, 'test_at'):
        raise ValueError('missing "model_dir" or "test_at" entry in the configuration (others)')

    model_dirs = context.config.others.model_dir
    if isinstance(model_dirs, str):
        model_dirs = [model_dirs]

    test_models = []
    for i, model_dir in enumerate(model_dirs):
        logging.info('load additional model [{}/{}] {}'.format(i+1, len(model_dirs), os.path.basename(model_dir)))
        mf = mgt.ModelFiles.from_model_dir(model_dir)
        checkpoint_path = mgt.model_service.find_checkpoint_file(mf.weight_checkpoint_dir, context.config.others.test_at)

        model = mgt.model_service.load_model_from_parameters(mf.model_path(), with_optimizer=False)
        mgt.model_service.load_checkpoint(checkpoint_path, model)
        test_model = model.to(context.device)

        test_model.eval()
        for params in test_model.parameters():
            params.requires_grad = False
        test_models.append(test_model)

    test_steps = [EnsemblePredictionStep(test_models), customstep.MultiPredictionSummary(),
                  PrepareSubjectStep()]
    subject_steps = [EvalSubjectStep()]

    subject_assembler = assembler.Subject2dAssembler()
    test = loop.Test(test_steps, subject_steps, subject_assembler, entries=None)

    hook = hooks.ReducedComposeTestLoopHook([hooks.ConsoleTestLogHook(),
                                             hooks.WriteTestMetricsCsvHook('metrics.csv'),
                                             WriteHook()
                                             ])
    test(context, build_test, hook=hook)


class EnsemblePredictionStep(step.BatchStep):

    def __init__(self, additional_models) -> None:
        super().__init__()
        self.additional_models = additional_models

    def __call__(self, batch_context: ctx.BatchContext, task_context: ctx.TaskContext, context: ctx.Context) -> None:
        if not isinstance(context, (ctx.TorchTrainContext, ctx.TorchTestContext)):
            raise ValueError(msg.get_type_error_msg(context, (ctx.TorchTrainContext, ctx.TorchTestContext)))

        batch_context.input['images'] = batch_context.input['images'].float().to(context.device)

        ensemble_probabilities = []
        logits = context.model(batch_context.input['images'])
        probs = F.softmax(logits, 1)
        ensemble_probabilities.append(probs)
        for additional_model in self.additional_models:
            logits = additional_model(batch_context.input['images'])
            probs = F.softmax(logits, 1)
            ensemble_probabilities.append(probs)

        ensemble_probabilities = torch.stack(ensemble_probabilities)
        batch_context.output['multi_probabilities'] = ensemble_probabilities


class EvalSubjectStep(step.SubjectStep):

    def __init__(self) -> None:
        super().__init__()
        self.evaluate = ev.ComposeEvaluation([ev.DiceNumpy()])

    def __call__(self, subject_context: ctx.SubjectContext, task_context: ctx.TaskContext, context: ctx.Context) -> None:
        probabilities = subject_context.subject_data['probabilities']
        prediction = np.argmax(probabilities, axis=-1)

        to_eval = {'prediction': prediction, 'probabilities': probabilities,
                   'target': subject_context.subject_data['labels'].squeeze(-1)}
        results = {}
        self.evaluate(to_eval, results)
        subject_context.metrics.update(results)


class PrepareSubjectStep(step.BatchStep):

    def __call__(self, batch_context: ctx.BatchContext, task_context: ctx.TaskContext, context: ctx.Context) -> None:
        batch_context.output['labels'] = batch_context.input['labels'].unsqueeze(1)  # re-add previously removed dim


class WriteHook(hooks.TestLoopHook):

    def __del__(self):
        thread.join_all()
        print('joined....')

    def on_test_subject_end(self, subject_context: ctx.SubjectContext, task_context: ctx.TaskContext,
                            context: ctx.TestContext):
        if not isinstance(context, ctx.TorchTestContext):
            raise ValueError(msg.get_type_error_msg(context, ctx.TorchTestContext))

        thread.do_work(WriteHook._on_test_subject_end,
                       subject_context, task_context, context, in_background=True)

    @staticmethod
    def _on_test_subject_end(subject_context: ctx.SubjectContext, task_context: ctx.TaskContext,
                             context: ctx.TorchTestContext):
        probabilities = subject_context.subject_data['probabilities']
        predictions = np.argmax(probabilities, axis=-1).astype(np.uint8)
        probabilities = probabilities[..., 1]  # foreground probabilities

        id_ = subject_context.subject_index

        probability_img = sitk.GetImageFromArray(probabilities)
        prediction_img = sitk.GetImageFromArray(predictions)

        sitk.WriteImage(probability_img, os.path.join(context.test_dir, '{}_probabilities.nii.gz'.format(id_)))
        sitk.WriteImage(prediction_img, os.path.join(context.test_dir, '{}_prediction.nii.gz'.format(id_)))

        files = context.test_data.dataset.get_files_by_id(id_)
        label_path = os.path.abspath(files['label_paths'])
        label_out_path = os.path.join(context.test_dir, os.path.basename(label_path))
        os.symlink(label_path, label_out_path)
        image_path = os.path.abspath(files['image_paths'])
        image_out_path = os.path.join(context.test_dir, os.path.basename(image_path))
        os.symlink(image_path, image_out_path)


if __name__ == '__main__':
    try:
        parser = argparse.ArgumentParser(description='ISIC test script (ensemble)')
        parser.add_argument('-config_file', type=str, help='the json file name containing the train configuration')
        args = parser.parse_args()
        main(args.config_file)
    finally:
        logging.exception('')  # log the exception
