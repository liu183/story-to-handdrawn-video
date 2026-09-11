import {CalculateMetadataFunction, Composition} from 'remotion';
import {StoryVideo, StoryboardVideo} from './StoryVideo';
import {storyboard, totalFrames, totalFramesFor} from './storyboard';
import {UploadedStoryVideo} from './UploadedStoryVideo';
import {
  uploadedStoryboard,
  uploadedTotalFrames,
} from './uploadedStoryboard';
import type {Storyboard} from './types';

/**
 * Language editions are rendered without touching any code: point `--props` at a
 * storyboard file and `calculateMetadata` derives fps, size and duration from it.
 *
 *   npx remotion render src/index.ts Edition out/edition.mp4 \
 *     --props=out/.../edition.props.json
 *
 * The props file is simply `{"storyboard": { ... }}`.
 */
export type EditionProps = {storyboard: Storyboard};

export const editionMetadata: CalculateMetadataFunction<EditionProps> = ({
  props,
}) => ({
  durationInFrames: totalFramesFor(props.storyboard),
  fps: props.storyboard.project.fps,
  width: props.storyboard.project.width,
  height: props.storyboard.project.height,
});

const Edition: React.FC<EditionProps> = ({storyboard: edition}) => (
  <StoryboardVideo value={edition} />
);

export const RemotionRoot: React.FC = () => {
  const {project} = storyboard;

  return (
    <>
      <Composition
        id="PictureSilent"
        component={StoryVideo}
        durationInFrames={totalFrames}
        fps={project.fps}
        width={project.width}
        height={project.height}
        defaultProps={{}}
      />
      <Composition
        id="Edition"
        component={Edition}
        calculateMetadata={editionMetadata}
        durationInFrames={totalFrames}
        fps={project.fps}
        width={project.width}
        height={project.height}
        defaultProps={{storyboard}}
      />
      <Composition
        id="UploadedPictureSilent"
        component={UploadedStoryVideo}
        durationInFrames={uploadedTotalFrames}
        fps={uploadedStoryboard.project.fps}
        width={uploadedStoryboard.project.width}
        height={uploadedStoryboard.project.height}
        defaultProps={{}}
      />
    </>
  );
};
