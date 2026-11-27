/* Protocol v1 decoder. Not built any more, kept as the reference for the v1 field format. */
int v1_decode(const unsigned char *frame) { return frame[0] << 8 | frame[1]; }
