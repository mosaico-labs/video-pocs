# Open questions

Questions still to discuss about how mosaicod should handle video data,
grouped by the three hypothesized ontology models of the
[datasets](README.md#dataset-structure).

All of them depend on the size of a gRPC message. mosaicod accepts messages
up to 50 MiB by default. This maximum can be set with
`MOSAICOD_GRPC_MAX_DECODE_MESSAGE_SIZE` to any value from 4 MiB to 512 MiB.
There is no minimum message size: a message can be as small as needed.

## Ontology models and serialization formats

### Image Sequence

- **Alpha channel.** The SEPE PNG images are RGBA, but image-sequence's
  server drops the alpha channel, because SVT-AV1 encodes only YUV. Nothing is
  lost on these images, since every pixel is opaque (alpha 255), but an image
  with transparent pixels would lose its transparency. Is dropping the alpha
  channel the right approach? If mosaicod should keep it, how?
- **Images larger than one message.** Each image is sent in one gRPC message.
  The dataset's PNG images are 20–33 MiB, so they all fit within the default
  50 MiB, but bigger images are rare, not impossible, and with the maximum
  set to 4 MiB none of them would fit. Should mosaicod accept such images, for
  example by splitting an image across several messages, or reject the whole
  sequence, as image-sequence's client does today before uploading anything?
- **Images much smaller than one message.** Small images, such as low
  resolution frames, fit many times in one message. Should the client send
  one image per message, as image-sequence does today, or pack several images
  into one message, up to the maximum 50 Mib, to send fewer messages?


### Video Sequences

A series of standalone videos that can be joined into one video. A single
video can be bigger than one gRPC message: the Earth files, for example, are
about 200 MB each. So:

1. The client splits each video into byte chunks and sends them to mosaicod;
2. mosaicod rebuilds each standalone video from its chunks;
3. mosaicod joins the sequence of standalone videos into one video;
4. mosaicod packages the video with MPEG-DASH and stores it in object
   storage.

- How should a standalone video be split across several messages, and what
  would its ontology model look like?

### Video Chunks

One big video, sent in pieces:

1. The client splits one big video into byte chunks and sends them to
   mosaicod, as video-parquet does today;
2. mosaicod rebuilds the video from its chunks;
3. mosaicod packages the video with MPEG-DASH and stores it in object
   storage.

- Can the ontology model follow video-parquet's approach, with some metadata
  added?
