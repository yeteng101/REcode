# External validation failure audit

Generated: `2026-11-12T03:13:59Z`

This audit uses Gold only for diagnosis. It does not modify the frozen protocol or predictions.

## Summary

| Category | Count |
|---|---:|
| CG-Bench unknown callsites | 41 |
| DataRaceBench unknown cases | 6 |
| CallGraphBench missed edges | 13 |
| MemSafety false positives | 8 |
| Skipped cases | 0 |

## Failure Categories

| Benchmark | Category | Count |
|---|---|---:|
| CG-Bench | callback_parameter_flow | 15 |
| CG-Bench | cast_or_global_initializer | 7 |
| CG-Bench | external_or_dynamic_registration | 5 |
| CG-Bench | struct_or_table_dispatch | 13 |
| CG-Bench | virtual_dispatch | 1 |
| DataRaceBench | affine_tiled_loop_dependence | 1 |
| DataRaceBench | indirect_index_or_pointer_alias | 5 |
| CallGraphBench | constructor_or_member_call | 3 |
| CallGraphBench | cross_file_callback_registration | 2 |
| CallGraphBench | function_pointer_dispatch | 4 |
| CallGraphBench | virtual_dispatch | 4 |
| SV-COMP MemSafety | container_of_pointer_arithmetic | 1 |
| SV-COMP MemSafety | linked_list_branch_ownership | 2 |
| SV-COMP MemSafety | linked_list_field_alias | 1 |
| SV-COMP MemSafety | linked_list_ownership_loop | 1 |
| SV-COMP MemSafety | multi_branch_pointer_adjustment | 1 |
| SV-COMP MemSafety | nested_struct_and_custom_calloc | 1 |
| SV-COMP MemSafety | struct_field_alias_and_early_return | 1 |

## CG-Bench Unknown

Total callsites: **41**

| Project | Callsite | Pointer | Type | Expected targets | Diagnosis |
|---|---|---|---|---|---|
| curl | `/home/yuanting/work/icall-benchmarks/llm/curl-build/lib/../../curl-8.5.0/lib/md5.c:628:6` | `*md5params->md5_init_func` | fnptr-cast | `my_md5_init` | cast_or_global_initializer |
| curl | `/home/yuanting/work/icall-benchmarks/llm/curl-build/lib/../../curl-8.5.0/lib/md5.c:641:3` | `*context->md5_hash->md5_update_func` | fnptr-cast | `my_md5_update` | cast_or_global_initializer |
| curl | `/home/yuanting/work/icall-benchmarks/llm/curl-build/lib/../../curl-8.5.0/lib/smtp.c:1847:28` | `Curl_cfree` | fnptr-only | `malloc` | cast_or_global_initializer |
| curl | `/home/yuanting/work/icall-benchmarks/llm/curl-build/lib/../../curl-8.5.0/lib/vauth/digest.c:789:3` | `hash` | fnptr-callback | `Curl_md5it, Curl_sha256it` | callback_parameter_flow |
| curl | `/home/yuanting/work/icall-benchmarks/llm/curl-build/lib/../../curl-8.5.0/lib/vauth/digest.c:806:3` | `convert_to_ascii` | fnptr-callback | `auth_digest_md5_to_ascii, auth_digest_sha256_to_ascii` | callback_parameter_flow |
| curl | `/home/yuanting/work/icall-benchmarks/llm/curl-build/lib/../../curl-8.5.0/lib/vtls/vtls.c:1018:14` | `Curl_ssl->sha256sum` | fnptr-global-struct | `ossl_sha256sum` | struct_or_table_dispatch |
| curl | `/home/yuanting/work/icall-benchmarks/llm/curl-build/lib/../../curl-8.5.0/lib/vtls/vtls.c:1308:10` | `Curl_ssl->send_plain` | fnptr-global-struct | `multissl_send_plain` | struct_or_table_dispatch |
| curl | `/home/yuanting/work/icall-benchmarks/llm/curl-build/src/../../curl-8.5.0/src/tool_writeout_json.c:112:8` | `mappings[i].writefunc` | fnptr-global-struct-array | `writeLong, writeOffset, writeString, writeTime` | struct_or_table_dispatch |
| ffmpeg | `/home/yuanting/work/icall-benchmarks/llm/ffmpeg-6.1.1/libavcodec/aaccoder.c:284:5` | `uantize_and_encode_band_cost_arr)[cb]` | fnptr-global-array | `quantize_and_encode_band_cost_ESC, quantize_and_encode_band_cost_ESC_RTZ, quantize_and_encode_band_cost_NOISE, quantize_and_encode_band_cost_NONE, quantize_and_encode_band_cost_SPAIR, quantize_and_encode_band_cost_SQUAD, quantize_and_encode_band_cost_STEREO, quantize_and_encode_band_cost_UPAIR, quantize_and_encode_band_cost_UQUAD, quantize_and_encode_band_cost_ZERO` | struct_or_table_dispatch |
| ffmpeg | `/home/yuanting/work/icall-benchmarks/llm/ffmpeg-6.1.1/libavcodec/allcodecs.c:957:14` | `x` | fnptr-callback | `av_codec_is_decoder, av_codec_is_encoder` | callback_parameter_flow |
| gcc | `/home/yuanting/work/icall-benchmarks/llm/gcc-clang-build/gcc/../../gcc-13.2.0/gcc/analyzer/region-model.h:698:12` | `get_state_map_by_name` | fnptr-virtual | `noop_region_model_context::get_state_map_by_name, region_model_context_decorator::get_state_map_by_name` | virtual_dispatch |
| gcc | `/home/yuanting/work/icall-benchmarks/llm/gcc-clang-build/gcc/../../gcc-13.2.0/gcc/gimple-fold.cc:7670:17` | `valueize` | fnptr-callback | `do_valueize, dom_valueize, pta_valueize, threadedge_valueize, valueize_op, valueize_val, vn_valueize` | callback_parameter_flow |
| gcc | `/home/yuanting/work/icall-benchmarks/llm/gcc-clang-build/gcc/./gtype-lto.h:1256:13` | `op` | fnptr-callback | `relocate_ptrs` | callback_parameter_flow |
| openssh | `/home/yuanting/work/icall-benchmarks/llm/openssh-build/../openssh-9.6p1/atomicio.c:71:23` | `cb` | fnptr-callback | `scpio, sftpio` | callback_parameter_flow |
| openssh | `/home/yuanting/work/icall-benchmarks/llm/openssh-build/../openssh-9.6p1/ssh-sk.c:148:12` | `ret->sk_api_version` | fnptr-dynamic-call | `sk_api_version` | external_or_dynamic_registration |
| openssh | `/home/yuanting/work/icall-benchmarks/llm/openssh-build/../openssh-9.6p1/sshkey.c:394:10` | `impl->funcs->size` | fnptr-global-struct-array | `ssh_rsa_size` | struct_or_table_dispatch |
| openssh | `/home/yuanting/work/icall-benchmarks/llm/openssh-build/../openssh-9.6p1/sshkey.c:620:7` | `impl->funcs->alloc` | fnptr-global-struct-array | `ssh_rsa_alloc` | struct_or_table_dispatch |
| redis | `/home/yuanting/work/icall-benchmarks/llm/redis-stable/deps/hiredis/read.c:323:23` | `r->fn->createDouble` | fnptr-global-struct | `createDoubleObject` | struct_or_table_dispatch |
| redis | `/home/yuanting/work/icall-benchmarks/llm/redis-stable/deps/jemalloc/include/jemalloc/internal/emap.h:329:21` | `ptr_getter` | fnptr-callback | `tcache_bin_flush_ptr_getter` | callback_parameter_flow |
| redis | `/home/yuanting/work/icall-benchmarks/llm/redis-stable/deps/jemalloc/src/jemalloc.c:3825:3` | `junk_alloc_callback` | fnptr-only | `default_junk_alloc` | cast_or_global_initializer |
| redis | `/home/yuanting/work/icall-benchmarks/llm/redis-stable/src/./connection.h:243:12` | `conn->type->set_write_handler` | fnptr-library | `connSocketSetWriteHandler, connTLSSetWriteHandler, connUnixSetWriteHandler` | external_or_dynamic_registration |
| redis | `/home/yuanting/work/icall-benchmarks/llm/redis-stable/src/defrag.c:583:23` | `element_cb` | fnptr-callback | `defragStreamConsumer, defragStreamConsumerGroup, defragStreamConsumerPendingEntry` | callback_parameter_flow |
| redis | `/home/yuanting/work/icall-benchmarks/llm/redis-stable/src/functions.c:428:13` | `engine->get_engine_memory_overhead` | fnptr-struct | `luaEngineMemoryOverhead` | struct_or_table_dispatch |
| redis | `/home/yuanting/work/icall-benchmarks/llm/redis-stable/src/module.c:11785:16` | `mt->mem_usage2` | fnptr-library | `NULL` | external_or_dynamic_registration |
| redis | `/home/yuanting/work/icall-benchmarks/llm/redis-stable/src/pqsort.c:95:38` | `cmp` | fnptr-callback | `sortCompare, sort_gp_asc, sort_gp_desc` | callback_parameter_flow |
| redis | `/home/yuanting/work/icall-benchmarks/llm/redis-stable/src/pubsub.c:167:24` | `type.clientPubSubChannels` | fnptr-global-struct | `getClientPubSubChannels` | struct_or_table_dispatch |
| redis | `/home/yuanting/work/icall-benchmarks/llm/redis-stable/src/quicklist.c:1543:21` | `saver` | fnptr-callback | `_quicklistSaver, listPopSaver` | callback_parameter_flow |
| redis | `/home/yuanting/work/icall-benchmarks/llm/redis-stable/src/redis-cli.c:8417:12` | `c->funcs->read` | fnptr-library | `redisNetRead` | external_or_dynamic_registration |
| redis | `/home/yuanting/work/icall-benchmarks/llm/redis-stable/src/server.c:4872:9` | `reply_function` | fnptr-callback | `addReplyCommandDocs, addReplyCommandInfo` | callback_parameter_flow |
| redis | `/home/yuanting/work/icall-benchmarks/llm/redis-stable/src/unix.c:134:12` | `connectionTypeTcp()->read` | fnptr-global-struct | `connTLSRead` | struct_or_table_dispatch |
| redis | `/home/yuanting/work/icall-benchmarks/llm/redis-stable/src/zmalloc.c:146:15` | `zmalloc_oom_handler` | fnptr-only | `zmalloc_default_oom` | cast_or_global_initializer |
| wrk | `/home/yuanting/work/icall-benchmarks/llm/wrk-4.2.0/obj/openssl-1.1.1i/crypto/engine/eng_dyn.c:480:10` | `ctx->bind_engine` | fnptr-dynamic-call | `bind_engine` | external_or_dynamic_registration |
| wrk | `/home/yuanting/work/icall-benchmarks/llm/wrk-4.2.0/obj/openssl-1.1.1i/ssl/statem/statem_clnt.c:3773:14` | `s->method->put_cipher_by_char` | fnptr-struct | `ssl3_put_cipher_by_char` | struct_or_table_dispatch |
| wrk | `/home/yuanting/work/icall-benchmarks/llm/wrk-4.2.0/src/wrk.c:551:17` | `fmt` | fnptr-callback | `format_metric, format_time_us` | callback_parameter_flow |
| zfs | `/home/yuanting/work/icall-benchmarks/llm/zfs-build/../zfs-2.2.2/cmd/zed/agents/fmd_api.c:328:3` | `ops->fmdo_close` | fnptr-cast | `zfs_fm_close` | cast_or_global_initializer |
| zfs | `/home/yuanting/work/icall-benchmarks/llm/zfs-build/../zfs-2.2.2/cmd/zstream/zstream_recompress.c:274:14` | `ci_decompress` | fnptr-global-struct-array | `gzip_decompress, lz4_decompress_zfs, lzjb_decompress, zfs_zstd_decompress, zle_decompress` | struct_or_table_dispatch |
| zfs | `/home/yuanting/work/icall-benchmarks/llm/zfs-build/../zfs-2.2.2/lib/libshare/nfs.c:202:11` | `cbk` | fnptr-callback | `nfs_copy_entries_cb, nfs_is_shared_cb` | callback_parameter_flow |
| zfs | `/home/yuanting/work/icall-benchmarks/llm/zfs-build/../zfs-2.2.2/module/icp/algs/modes/ccm.c:232:3` | `xor_block` | fnptr-callback | `aes_xor_block` | callback_parameter_flow |
| zfs | `/home/yuanting/work/icall-benchmarks/llm/zfs-build/../zfs-2.2.2/module/icp/algs/modes/ccm.c:97:3` | `encrypt_block` | fnptr-callback | `aes_encrypt_block` | callback_parameter_flow |
| zfs | `/home/yuanting/work/icall-benchmarks/llm/zfs-build/../zfs-2.2.2/module/icp/algs/sha2/sha2_generic.c:237:3` | `ops->transform` | fnptr-global-struct-array | `sha256_generic, sha512_generic, tf_sha256_transform_x64, tf_sha512_transform_x64` | struct_or_table_dispatch |
| zfs | `/home/yuanting/work/icall-benchmarks/llm/zfs-build/../zfs-2.2.2/module/zcommon/zfs_fletcher.c:887:4` | `ops->compute_native` | fnptr-cast | `fletcher_4_scalar_native, fletcher_4_superscalar4_native, fletcher_4_superscalar_native` | cast_or_global_initializer |

### CG-Bench Extracted Evidence

#### `cgbench_c038b1cbb3b4`

- Project/type: `curl` / `fnptr-cast`; pointer: `*md5params->md5_init_func`
- Expected targets: `my_md5_init`; candidate reasons: `{}`
- Tool trace: `inspect_pointer_assignments=ok, inspect_pointer_tables=ok, inspect_struct_tables=ok, inspect_callback_registration=ok, inspect_macros=ok, finalize_target_set=ok`
- Source context:
  - `if((*md5params->md5_init_func)(ctxt->md5_hashctx)) {`
  - `CURLX_FUNCTION_CAST(Curl_MD5_init_func, my_md5_init),`

#### `cgbench_c527147ad40c`

- Project/type: `curl` / `fnptr-cast`; pointer: `*context->md5_hash->md5_update_func`
- Expected targets: `my_md5_update`; candidate reasons: `{}`
- Tool trace: `inspect_pointer_assignments=ok, inspect_pointer_tables=ok, inspect_struct_tables=ok, inspect_callback_registration=ok, inspect_macros=ok, finalize_target_set=ok`
- Source context:
  - `(*context->md5_hash->md5_update_func)(context->md5_hashctx, data, len);`
  - `CURLX_FUNCTION_CAST(Curl_MD5_update_func, my_md5_update),`

#### `cgbench_73b0ece4ba34`

- Project/type: `curl` / `fnptr-only`; pointer: `Curl_cfree`
- Expected targets: `malloc`; candidate reasons: `{}`
- Tool trace: `inspect_pointer_assignments=ok, inspect_pointer_tables=ok, inspect_struct_tables=ok, inspect_callback_registration=ok, inspect_macros=ok, finalize_target_set=ok`
- Source context:
  - `scratch = newscratch = malloc(2 * data->set.upload_buffer_size);`
  - `#define malloc(size) Curl_cmalloc(size)`

#### `cgbench_a6189a36a435`

- Project/type: `curl` / `fnptr-callback`; pointer: `hash`
- Expected targets: `Curl_md5it, Curl_sha256it`; candidate reasons: `{}`
- Tool trace: `inspect_pointer_assignments=ok, inspect_pointer_tables=ok, inspect_struct_tables=ok, inspect_callback_registration=ok, inspect_macros=ok, finalize_target_set=ok`
- Source context:
  - `CURLcode (*hash)(unsigned char *, const unsigned char *,`
  - `if(!hashthis)`
  - `hash(hashbuf, (unsigned char *) hashthis, strlen(hashthis));`
  - `Curl_md5it);`
  - `Curl_sha256it);`

#### `cgbench_d9bc6c3944fb`

- Project/type: `curl` / `fnptr-callback`; pointer: `convert_to_ascii`
- Expected targets: `auth_digest_md5_to_ascii, auth_digest_sha256_to_ascii`; candidate reasons: `{}`
- Tool trace: `inspect_pointer_assignments=ok, inspect_pointer_tables=ok, inspect_struct_tables=ok, inspect_callback_registration=ok, inspect_macros=ok, finalize_target_set=ok`
- Source context:
  - `void (*convert_to_ascii)(unsigned char *, unsigned char *),`
  - `convert_to_ascii(hashbuf, (unsigned char *)userh);`
  - `auth_digest_md5_to_ascii,`
  - `auth_digest_sha256_to_ascii,`

#### `cgbench_2ac1ee879e1f`

- Project/type: `curl` / `fnptr-global-struct`; pointer: `Curl_ssl->sha256sum`
- Expected targets: `ossl_sha256sum`; candidate reasons: `{}`
- Tool trace: `inspect_pointer_assignments=ok, inspect_pointer_tables=ok, inspect_struct_tables=ok, inspect_callback_registration=ok, inspect_macros=ok, finalize_target_set=ok`
- Source context:
  - `/* compute sha256sum of public key */`
  - `sha256sumdigest = malloc(CURL_SHA256_DIGEST_LENGTH);`
  - `if(!sha256sumdigest)`
  - `encode = Curl_ssl->sha256sum(pubkey, pubkeylen,`
  - `sha256sumdigest, CURL_SHA256_DIGEST_LENGTH);`

#### `cgbench_88a544dc3e55`

- Project/type: `curl` / `fnptr-global-struct`; pointer: `Curl_ssl->send_plain`
- Expected targets: `multissl_send_plain`; candidate reasons: `{}`
- Tool trace: `inspect_pointer_assignments=ok, inspect_pointer_tables=ok, inspect_struct_tables=ok, inspect_callback_registration=ok, inspect_macros=ok, finalize_target_set=ok`
- Source context:
  - `static ssize_t multissl_send_plain(struct Curl_cfilter *cf,`
  - `return Curl_ssl->send_plain(cf, data, mem, len, code);`
  - `multissl_send_plain, /* send data to encrypt */`
  - `ssize_t (*send_plain)(struct Curl_cfilter *cf, struct Curl_easy *data,`

#### `cgbench_b958c5b6832f`

- Project/type: `curl` / `fnptr-global-struct-array`; pointer: `mappings[i].writefunc`
- Expected targets: `writeLong, writeOffset, writeString, writeTime`; candidate reasons: `{}`
- Tool trace: `inspect_pointer_assignments=ok, inspect_pointer_tables=ok, inspect_struct_tables=ok, inspect_callback_registration=ok, inspect_macros=ok, finalize_target_set=ok`
- Source context:
  - `if(mappings[i].writefunc &&`
  - `mappings[i].writefunc(stream, &mappings[i], per, per_result, true))`
  - `{"certs", VAR_CERT, CURLINFO_NONE, writeString},`
  - `CURLINFO_PROXY_SSL_VERIFYRESULT, writeLong},`
  - `{"scheme", VAR_SCHEME, CURLINFO_SCHEME, writeString},`

#### `cgbench_286eb9769472`

- Project/type: `ffmpeg` / `fnptr-global-array`; pointer: `uantize_and_encode_band_cost_arr)[cb]`
- Expected targets: `quantize_and_encode_band_cost_ESC, quantize_and_encode_band_cost_ESC_RTZ, quantize_and_encode_band_cost_NOISE, quantize_and_encode_band_cost_NONE, quantize_and_encode_band_cost_SPAIR, quantize_and_encode_band_cost_SQUAD, quantize_and_encode_band_cost_STEREO, quantize_and_encode_band_cost_UPAIR, quantize_and_encode_band_cost_UQUAD, quantize_and_encode_band_cost_ZERO`; candidate reasons: `{}`
- Tool trace: `inspect_pointer_assignments=ok, inspect_pointer_tables=ok, inspect_struct_tables=ok, inspect_callback_registration=ok, inspect_macros=ok, finalize_target_set=ok`
- Source context:
  - `(rtz ? quantize_and_encode_band_cost_rtz_arr : quantize_and_encode_band_cost_arr)[cb](s, pb, in, out, NULL, size, scale_idx, cb,`
  - `static const quantize_and_encode_band_func quantize_and_encode_band_cost_arr[] =`
  - `quantize_and_encode_band_cost_ZERO,`
  - `quantize_and_encode_band_cost_SQUAD,`
  - `quantize_and_encode_band_cost_SQUAD,`

#### `cgbench_d5225683ff9f`

- Project/type: `ffmpeg` / `fnptr-callback`; pointer: `x`
- Expected targets: `av_codec_is_decoder, av_codec_is_encoder`; candidate reasons: `{}`
- Tool trace: `inspect_pointer_assignments=ok, inspect_pointer_tables=ok, inspect_struct_tables=ok, inspect_callback_registration=ok, inspect_macros=ok, finalize_target_set=ok`
- Source context:
  - `static const AVCodec *find_codec(enum AVCodecID id, int (*x)(const AVCodec *))`
  - `const AVCodec *p, *experimental = NULL;`
  - `if (!x(p))`
  - `if (p->capabilities & AV_CODEC_CAP_EXPERIMENTAL && !experimental) {`
  - `experimental = p;`

#### `cgbench_e5f16859dd08`

- Project/type: `gcc` / `fnptr-virtual`; pointer: `get_state_map_by_name`
- Expected targets: `noop_region_model_context::get_state_map_by_name, region_model_context_decorator::get_state_map_by_name`; candidate reasons: `{}`
- Tool trace: `inspect_pointer_assignments=ok, inspect_pointer_tables=ok, inspect_struct_tables=ok, inspect_callback_registration=ok, inspect_macros=ok, finalize_target_set=ok`
- Source context:
  - `get_state_map_by_name (const char *name,`
  - `return get_state_map_by_name ("file-descriptor", out_smap, out_sm,`
  - `return get_state_map_by_name ("malloc", out_smap, out_sm, out_sm_idx, NULL);`
  - `return get_state_map_by_name ("taint", out_smap, out_sm, out_sm_idx, NULL);`
  - `bool get_state_map_by_name (const char *,`

#### `cgbench_12fdcdae8796`

- Project/type: `gcc` / `fnptr-callback`; pointer: `valueize`
- Expected targets: `do_valueize, dom_valueize, pta_valueize, threadedge_valueize, valueize_op, valueize_val, vn_valueize`; candidate reasons: `{}`
- Tool trace: `inspect_pointer_assignments=ok, inspect_pointer_tables=ok, inspect_struct_tables=ok, inspect_callback_registration=ok, inspect_macros=ok, finalize_target_set=ok`
- Source context:
  - `gimple_fold_stmt_to_constant_1 (gimple *stmt, tree (*valueize) (tree),`
  - `tree (*gvalueize) (tree))`
  - `tree op0 = (*valueize) (arg0);`
  - `tree op1 = (*valueize) (arg1);`
  - `gimple_fold_stmt_to_constant (gimple *stmt, tree (*valueize) (tree))`

#### `cgbench_d7c885aa3033`

- Project/type: `gcc` / `fnptr-callback`; pointer: `op`
- Expected targets: `relocate_ptrs`; candidate reasons: `{}`
- Tool trace: `inspect_pointer_assignments=ok, inspect_pointer_tables=ok, inspect_struct_tables=ok, inspect_callback_registration=ok, inspect_macros=ok, finalize_target_set=ok`
- Source context:
  - `ATTRIBUTE_UNUSED gt_pointer_operator op,`
  - `op (&((*x).generic.typed.type), NULL, cookie);`
  - `relocate_ptrs, &state);`
  - `relocate_ptrs (void *ptr_p, void *real_ptr_p, void *state_p)`

#### `cgbench_920dffb7f284`

- Project/type: `openssh` / `fnptr-callback`; pointer: `cb`
- Expected targets: `scpio, sftpio`; candidate reasons: `{}`
- Tool trace: `inspect_pointer_assignments=ok, inspect_pointer_tables=ok, inspect_struct_tables=ok, inspect_callback_registration=ok, inspect_macros=ok, finalize_target_set=ok`
- Source context:
  - `int (*cb)(void *, size_t), void *cb_arg)`
  - `if (cb != NULL && cb(cb_arg, 0) == -1) {`
  - `if (cb != NULL && cb(cb_arg, (size_t)res) == -1) {`
  - `if (atomicio6(vwrite, remout, bp->buf, amt, scpio,`
  - `scpio(void *_cnt, size_t s)`

#### `cgbench_fad508166008`

- Project/type: `openssh` / `fnptr-dynamic-call`; pointer: `ret->sk_api_version`
- Expected targets: `sk_api_version`; candidate reasons: `{}`
- Tool trace: `inspect_pointer_assignments=ok, inspect_pointer_tables=ok, inspect_struct_tables=ok, inspect_callback_registration=ok, inspect_macros=ok, finalize_target_set=ok`
- Source context:
  - `if ((ret->sk_api_version = dlsym(ret->dlhandle,`
  - `"sk_api_version")) == NULL) {`
  - `error("Provider \"%s\" dlsym(sk_api_version) failed: %s",`
  - `version = ret->sk_api_version();`

#### `cgbench_e4e61b8f77db`

- Project/type: `openssh` / `fnptr-global-struct-array`; pointer: `impl->funcs->size`
- Expected targets: `ssh_rsa_size`; candidate reasons: `{}`
- Tool trace: `inspect_pointer_assignments=ok, inspect_pointer_tables=ok, inspect_struct_tables=ok, inspect_callback_registration=ok, inspect_macros=ok, finalize_target_set=ok`
- Source context:
  - `sshkey_size(const struct sshkey *k)`
  - `if (impl->funcs->size != NULL)`
  - `return impl->funcs->size(k);`
  - `/* .size = */ ssh_rsa_size,`

#### `cgbench_92d4c609fb7a`

- Project/type: `openssh` / `fnptr-global-struct-array`; pointer: `impl->funcs->alloc`
- Expected targets: `ssh_rsa_alloc`; candidate reasons: `{}`
- Tool trace: `inspect_pointer_assignments=ok, inspect_pointer_tables=ok, inspect_struct_tables=ok, inspect_callback_registration=ok, inspect_macros=ok, finalize_target_set=ok`
- Source context:
  - `if ((k = calloc(1, sizeof(*k))) == NULL)`
  - `if (impl != NULL && impl->funcs->alloc != NULL) {`
  - `if (impl->funcs->alloc(k) != 0) {`
  - `/* .alloc = */ ssh_rsa_alloc,`

#### `cgbench_4f8fe57846e8`

- Project/type: `redis` / `fnptr-global-struct`; pointer: `r->fn->createDouble`
- Expected targets: `createDoubleObject`; candidate reasons: `{}`
- Tool trace: `inspect_pointer_assignments=ok, inspect_pointer_tables=ok, inspect_struct_tables=ok, inspect_callback_registration=ok, inspect_macros=ok, finalize_target_set=ok`
- Source context:
  - `if (r->fn && r->fn->createDouble) {`
  - `obj = r->fn->createDouble(cur,d,buf,len);`
  - `createDoubleObject,`

#### `cgbench_a75f1c2345fa`

- Project/type: `redis` / `fnptr-callback`; pointer: `ptr_getter`
- Expected targets: `tcache_bin_flush_ptr_getter`; candidate reasons: `{}`
- Tool trace: `inspect_pointer_assignments=ok, inspect_pointer_tables=ok, inspect_struct_tables=ok, inspect_callback_registration=ok, inspect_macros=ok, finalize_target_set=ok`
- Source context:
  - `emap_ptr_getter ptr_getter, void *ptr_getter_ctx,`
  - `const void *ptr = ptr_getter(ptr_getter_ctx, i);`
  - `&tcache_bin_flush_ptr_getter, (void *)arr,`

#### `cgbench_f04c1f6109ea`

- Project/type: `redis` / `fnptr-only`; pointer: `junk_alloc_callback`
- Expected targets: `default_junk_alloc`; candidate reasons: `{}`
- Tool trace: `inspect_pointer_assignments=ok, inspect_pointer_tables=ok, inspect_struct_tables=ok, inspect_callback_registration=ok, inspect_macros=ok, finalize_target_set=ok`
- Source context:
  - `junk_alloc_callback(excess_start, excess_len);`
  - `void (*junk_alloc_callback)(void *ptr, size_t size) = &default_junk_alloc;`

#### `cgbench_9d90a3631a4c`

- Project/type: `redis` / `fnptr-library`; pointer: `conn->type->set_write_handler`
- Expected targets: `connSocketSetWriteHandler, connTLSSetWriteHandler, connUnixSetWriteHandler`; candidate reasons: `{}`
- Tool trace: `inspect_pointer_assignments=ok, inspect_pointer_tables=ok, inspect_struct_tables=ok, inspect_callback_registration=ok, inspect_macros=ok, finalize_target_set=ok`
- Source context:
  - `return conn->type->set_write_handler(conn, func, barrier);`
  - `.set_write_handler = connSocketSetWriteHandler,`
  - `.set_write_handler = connTLSSetWriteHandler,`
  - `.set_write_handler = connUnixSetWriteHandler,`

#### `cgbench_7fa34f93e778`

- Project/type: `redis` / `fnptr-callback`; pointer: `element_cb`
- Expected targets: `defragStreamConsumer, defragStreamConsumerGroup, defragStreamConsumerPendingEntry`; candidate reasons: `{}`
- Tool trace: `inspect_pointer_assignments=ok, inspect_pointer_tables=ok, inspect_struct_tables=ok, inspect_callback_registration=ok, inspect_macros=ok, finalize_target_set=ok`
- Source context:
  - `void defragRadixTree(rax **raxref, int defrag_data, raxDefragFunction *element_cb, void *element_cb_data) {`
  - `if (element_cb)`
  - `newdata = element_cb(&ri, element_cb_data);`
  - `void* defragStreamConsumer(raxIterator *ri, void *privdata) {`
  - `defragRadixTree(&c->pel, 0, defragStreamConsumerPendingEntry, &pel_ctx);`

#### `cgbench_5d6eab0ac06f`

- Project/type: `redis` / `fnptr-struct`; pointer: `engine->get_engine_memory_overhead`
- Expected targets: `luaEngineMemoryOverhead`; candidate reasons: `{}`
- Tool trace: `inspect_pointer_assignments=ok, inspect_pointer_tables=ok, inspect_struct_tables=ok, inspect_callback_registration=ok, inspect_macros=ok, finalize_target_set=ok`
- Source context:
  - `engine->get_engine_memory_overhead(engine->engine_ctx);`
  - `.get_engine_memory_overhead = luaEngineMemoryOverhead,`

#### `cgbench_167041fb0761`

- Project/type: `redis` / `fnptr-library`; pointer: `mt->mem_usage2`
- Expected targets: `NULL`; candidate reasons: `{}`
- Tool trace: `inspect_pointer_assignments=ok, inspect_pointer_tables=ok, inspect_struct_tables=ok, inspect_callback_registration=ok, inspect_macros=ok, finalize_target_set=ok`
- Source context:
  - `if (mt->mem_usage2 != NULL) {`
  - `RedisModuleKeyOptCtx ctx = {key, NULL, dbid, -1};`
  - `size = mt->mem_usage2(&ctx, mv->value, sample_size);`
  - `} else if (mt->mem_usage != NULL) {`

#### `cgbench_3f6bacc0863e`

- Project/type: `redis` / `fnptr-callback`; pointer: `cmp`
- Expected targets: `sortCompare, sort_gp_asc, sort_gp_desc`; candidate reasons: `{}`
- Tool trace: `inspect_pointer_assignments=ok, inspect_pointer_tables=ok, inspect_struct_tables=ok, inspect_callback_registration=ok, inspect_macros=ok, finalize_target_set=ok`
- Source context:
  - `int (*cmp) (const void *, const void *))`
  - `return cmp(a, b) < 0 ?`
  - `(cmp(b, c) < 0 ? b : (cmp(a, c) < 0 ? c : a ))`
  - `:(cmp(b, c) > 0 ? b : (cmp(a, c) < 0 ? a : c ));`
  - `int (*cmp) (const void *, const void *), void *lrange, void *rrange)`

#### `cgbench_b60c75ab100d`

- Project/type: `redis` / `fnptr-global-struct`; pointer: `type.clientPubSubChannels`
- Expected targets: `getClientPubSubChannels`; candidate reasons: `{}`
- Tool trace: `inspect_pointer_assignments=ok, inspect_pointer_tables=ok, inspect_struct_tables=ok, inspect_callback_registration=ok, inspect_macros=ok, finalize_target_set=ok`
- Source context:
  - `.clientPubSubChannels = getClientPubSubChannels,`
  - `if (dictAdd(type.clientPubSubChannels(c),channel,NULL) == DICT_OK) {`

#### `cgbench_a3769a595743`

- Project/type: `redis` / `fnptr-callback`; pointer: `saver`
- Expected targets: `_quicklistSaver, listPopSaver`; candidate reasons: `{}`
- Tool trace: `inspect_pointer_assignments=ok, inspect_pointer_tables=ok, inspect_struct_tables=ok, inspect_callback_registration=ok, inspect_macros=ok, finalize_target_set=ok`
- Source context:
  - `void *(*saver)(unsigned char *data, size_t sz)) {`
  - `*data = saver(node->entry, node->sz);`
  - `NULL, &vlong, listPopSaver)) {`
  - `_quicklistSaver);`

#### `cgbench_50c70a205423`

- Project/type: `redis` / `fnptr-library`; pointer: `c->funcs->read`
- Expected targets: `redisNetRead`; candidate reasons: `{}`
- Tool trace: `inspect_pointer_assignments=ok, inspect_pointer_tables=ok, inspect_struct_tables=ok, inspect_callback_registration=ok, inspect_macros=ok, finalize_target_set=ok`
- Source context:
  - `/* Read raw bytes through a redisContext. The read operation is not greedy`
  - `static ssize_t readConn(redisContext *c, char *buf, size_t len)`
  - `return c->funcs->read(c, buf, len);`
  - `c->reader = redisReaderCreate();`
  - `if (c->obuf == NULL \|\| c->reader == NULL) {`

#### `cgbench_1ccadb4300ea`

- Project/type: `redis` / `fnptr-callback`; pointer: `reply_function`
- Expected targets: `addReplyCommandDocs, addReplyCommandInfo`; candidate reasons: `{}`
- Tool trace: `inspect_pointer_assignments=ok, inspect_pointer_tables=ok, inspect_struct_tables=ok, inspect_callback_registration=ok, inspect_macros=ok, finalize_target_set=ok`
- Source context:
  - `void addReplyCommandSubCommands(client *c, struct redisCommand *cmd, void (*reply_function)(client*, struct redisCommand*), int use_map) {`
  - `reply_function(c, sub);`
  - `void addReplyCommandInfo(client *c, struct redisCommand *cmd) {`
  - `addReplyCommandSubCommands(c, cmd, addReplyCommandInfo, 0);`
  - `void addReplyCommandDocs(client *c, struct redisCommand *cmd) {`

#### `cgbench_36ab6a76a22b`

- Project/type: `redis` / `fnptr-global-struct`; pointer: `connectionTypeTcp()->read`
- Expected targets: `connTLSRead`; candidate reasons: `{}`
- Tool trace: `inspect_pointer_assignments=ok, inspect_pointer_tables=ok, inspect_struct_tables=ok, inspect_callback_registration=ok, inspect_macros=ok, finalize_target_set=ok`
- Source context:
  - `return connectionTypeTcp()->read(conn, buf, buf_len);`
  - `.read = connTLSRead,`
  - `.read = connTLSRead,`

#### `cgbench_303b6ec72a2e`

- Project/type: `redis` / `fnptr-only`; pointer: `zmalloc_oom_handler`
- Expected targets: `zmalloc_default_oom`; candidate reasons: `{}`
- Tool trace: `inspect_pointer_assignments=ok, inspect_pointer_tables=ok, inspect_struct_tables=ok, inspect_callback_registration=ok, inspect_macros=ok, finalize_target_set=ok`
- Source context:
  - `if (!ptr) zmalloc_oom_handler(size);`
  - `static void (*zmalloc_oom_handler)(size_t) = zmalloc_default_oom;`

#### `cgbench_cae6a58feea6`

- Project/type: `wrk` / `fnptr-dynamic-call`; pointer: `ctx->bind_engine`
- Expected targets: `bind_engine`; candidate reasons: `{}`
- Tool trace: `inspect_pointer_assignments=ok, inspect_pointer_tables=ok, inspect_struct_tables=ok, inspect_callback_registration=ok, inspect_macros=ok, finalize_target_set=ok`
- Source context:
  - `c->DYNAMIC_F2 = "bind_engine";`
  - `(ctx->bind_engine =`
  - `(dynamic_bind_engine) DSO_bind_func(ctx->dynamic_dso,`
  - `ctx->bind_engine = NULL;`
  - `ctx->bind_engine = NULL;`

#### `cgbench_1e7a84549bc1`

- Project/type: `wrk` / `fnptr-struct`; pointer: `s->method->put_cipher_by_char`
- Expected targets: `ssl3_put_cipher_by_char`; candidate reasons: `{}`
- Tool trace: `inspect_pointer_assignments=ok, inspect_pointer_tables=ok, inspect_struct_tables=ok, inspect_callback_registration=ok, inspect_macros=ok, finalize_target_set=ok`
- Source context:
  - `if (!s->method->put_cipher_by_char(c, pkt, &len)) {`
  - `ssl3_put_cipher_by_char, \`
  - `int (*put_cipher_by_char) (const SSL_CIPHER *cipher, WPACKET *pkt,`
  - `int ssl3_put_cipher_by_char(const SSL_CIPHER *c, WPACKET *pkt, size_t *len)`

#### `cgbench_66e07f14192d`

- Project/type: `wrk` / `fnptr-callback`; pointer: `fmt`
- Expected targets: `format_metric, format_time_us`; candidate reasons: `{}`
- Tool trace: `inspect_pointer_assignments=ok, inspect_pointer_tables=ok, inspect_struct_tables=ok, inspect_callback_registration=ok, inspect_macros=ok, finalize_target_set=ok`
- Source context:
  - `static void print_units(long double n, char *(*fmt)(long double), int width) {`
  - `char *msg = fmt(n);`
  - `print_units(n, format_time_us, 10);`
  - `static void print_stats(char *name, stats *stats, char *(*fmt)(long double)) {`
  - `print_units(mean, fmt, 8);`

#### `cgbench_ee0e24f737c4`

- Project/type: `zfs` / `fnptr-cast`; pointer: `ops->fmdo_close`
- Expected targets: `zfs_fm_close`; candidate reasons: `{}`
- Tool trace: `inspect_pointer_assignments=ok, inspect_pointer_tables=ok, inspect_struct_tables=ok, inspect_callback_registration=ok, inspect_macros=ok, finalize_target_set=ok`
- Source context:
  - `if (ops->fmdo_close != NULL)`
  - `ops->fmdo_close(hdl, cp);`
  - `zfs_fm_close, /* fmdo_close */`
  - `zfs_fm_close(fmd_hdl_t *hdl, fmd_case_t *cs)`

#### `cgbench_f8c74ea876ec`

- Project/type: `zfs` / `fnptr-global-struct-array`; pointer: `ci_decompress`
- Expected targets: `gzip_decompress, lz4_decompress_zfs, lzjb_decompress, zfs_zstd_decompress, zle_decompress`; candidate reasons: `{}`
- Tool trace: `inspect_pointer_assignments=ok, inspect_pointer_tables=ok, inspect_struct_tables=ok, inspect_callback_registration=ok, inspect_macros=ok, finalize_target_set=ok`
- Source context:
  - `if (dinfo->ci_decompress != NULL) {`
  - `if (0 != dinfo->ci_decompress(cbuf, dbuf,`
  - `{"lzjb", 0, lzjb_compress, lzjb_decompress, NULL},`
  - `{"gzip-1", 1, gzip_compress, gzip_decompress, NULL},`
  - `{"gzip-2", 2, gzip_compress, gzip_decompress, NULL},`

#### `cgbench_9237e9fc3c2c`

- Project/type: `zfs` / `fnptr-callback`; pointer: `cbk`
- Expected targets: `nfs_copy_entries_cb, nfs_is_shared_cb`; candidate reasons: `{}`
- Tool trace: `inspect_pointer_assignments=ok, inspect_pointer_tables=ok, inspect_struct_tables=ok, inspect_callback_registration=ok, inspect_macros=ok, finalize_target_set=ok`
- Source context:
  - `boolean_t (*cbk)(void *userdata, char *line, boolean_t found_mountpoint),`
  - `cont = cbk(userdata, buf,`
  - `exports, mountpoint, nfs_copy_entries_cb, newfp);`
  - `nfs_is_shared_cb, &found);`

#### `cgbench_722a5d7bed3d`

- Project/type: `zfs` / `fnptr-callback`; pointer: `xor_block`
- Expected targets: `aes_xor_block`; candidate reasons: `{}`
- Tool trace: `inspect_pointer_assignments=ok, inspect_pointer_tables=ok, inspect_struct_tables=ok, inspect_callback_registration=ok, inspect_macros=ok, finalize_target_set=ok`
- Source context:
  - `void (*xor_block)(uint8_t *, uint8_t *))`
  - `xor_block(macp, mac_buf);`
  - `aes_xor_block);`
  - `AES_BLOCK_LEN, aes_encrypt_block, aes_xor_block);`
  - `AES_BLOCK_LEN, aes_encrypt_block, aes_xor_block);`

#### `cgbench_07f3a542ba5f`

- Project/type: `zfs` / `fnptr-callback`; pointer: `encrypt_block`
- Expected targets: `aes_encrypt_block`; candidate reasons: `{}`
- Tool trace: `inspect_pointer_assignments=ok, inspect_pointer_tables=ok, inspect_struct_tables=ok, inspect_callback_registration=ok, inspect_macros=ok, finalize_target_set=ok`
- Source context:
  - `int (*encrypt_block)(const void *, const uint8_t *, uint8_t *),`
  - `encrypt_block(ctx->ccm_keysched, mac_buf, mac_buf);`
  - `encrypt_block(ctx->ccm_keysched, (uint8_t *)ctx->ccm_cb,`
  - `AES_BLOCK_LEN, aes_encrypt_block, aes_xor_block);`
  - `out, AES_BLOCK_LEN, aes_encrypt_block, aes_copy_block,`

#### `cgbench_b9fb3964ee7b`

- Project/type: `zfs` / `fnptr-global-struct-array`; pointer: `ops->transform`
- Expected targets: `sha256_generic, sha512_generic, tf_sha256_transform_x64, tf_sha512_transform_x64`; candidate reasons: `{}`
- Tool trace: `inspect_pointer_assignments=ok, inspect_pointer_tables=ok, inspect_struct_tables=ok, inspect_callback_registration=ok, inspect_macros=ok, finalize_target_set=ok`
- Source context:
  - `ops->transform(ctx->state, m, 1);`
  - `ops->transform(ctx->state, data, blocks);`
  - `&sha256_generic_impl,`
  - `const sha256_ops_t sha256_generic_impl = {`
  - `.transform = sha256_generic,`

#### `cgbench_7c4366c0fd2d`

- Project/type: `zfs` / `fnptr-cast`; pointer: `ops->compute_native`
- Expected targets: `fletcher_4_scalar_native, fletcher_4_superscalar4_native, fletcher_4_superscalar_native`; candidate reasons: `{}`
- Tool trace: `inspect_pointer_assignments=ok, inspect_pointer_tables=ok, inspect_struct_tables=ok, inspect_callback_registration=ok, inspect_macros=ok, finalize_target_set=ok`
- Source context:
  - `ops->compute_native(ctx, data, asize);`
  - `.compute_native = fletcher_4_scalar_native,`
  - `.compute_native = fletcher_4_superscalar_native,`
  - `.compute_native = fletcher_4_superscalar4_native,`


## DataRaceBench Unknown

Total cases: **6**

| Case | Expected race | Diagnosis | Existing reason |
|---|---:|---|---|
| DRB005-indirectaccess1-orig-yes | true | indirect_index_or_pointer_alias | shared accesses exist but index disjointness cannot be proven: xa1[idx], xa2[idx] |
| DRB006-indirectaccess2-orig-yes | true | indirect_index_or_pointer_alias | shared accesses exist but index disjointness cannot be proven: xa1[idx], xa2[idx] |
| DRB007-indirectaccess3-orig-yes | true | indirect_index_or_pointer_alias | shared accesses exist but index disjointness cannot be proven: xa1[idx], xa2[idx] |
| DRB008-indirectaccess4-orig-yes | true | indirect_index_or_pointer_alias | shared accesses exist but index disjointness cannot be proven: xa1[idx], xa2[idx] |
| DRB044-adi-tile-no | false | affine_tiled_loop_dependence | shared accesses exist but index disjointness cannot be proven: B[c15 \| n - 3 - c9], X[c15 \| n - 2 - c9], X[c15 \| n - 2 - c9 - 1], X[c15 \| n - c9 - 2] |
| DRB052-indirectaccesssharebase-orig-no | false | indirect_index_or_pointer_alias | shared accesses exist but index disjointness cannot be proven: xa1[idx], xa2[idx] |

### DataRaceBench Extracted Evidence

#### `DRB005-indirectaccess1-orig-yes`

- Index set: count=180, range=521..2013, pairs at distance 12: `911->923`
- Source context:
  - `48: Two pointers (xa1 and xa2) have a pair of values with a distance of 12.`
  - `59: Data race pair: xa1[idx]@128:5:W vs. xa2[idx]@129:5:W`
  - `66: int indexSet[N] = {`
  - `105: // max index value is 2013. +12 to obtain a valid xa2[idx] after xa1+12.`
  - `114: double * xa1 = base;`
  - `115: double * xa2 = xa1 + 12;`
  - `118: // initialize segments touched by indexSet`
  - `121: base[i]=0.5*i;`
  - `124: #pragma omp parallel for schedule(static,1)`
  - `127: int idx = indexSet[i];`
  - `128: xa1[idx]+= 1.0 + i;`
  - `129: xa2[idx]+= 3.0 + i;`
  - `132: printf("x1[999]=%f xa2[1285]=%f\n", xa1[999], xa2[1285]);`

#### `DRB006-indirectaccess2-orig-yes`

- Index set: count=180, range=521..2013, pairs at distance 12: `521->533`
- Source context:
  - `51: indexSet[5]- indexSet[0] = 533 - 521 = 12`
  - `61: Data race pair: xa1[idx]@128:5:W vs. xa2[idx]@129:5:W`
  - `68: int indexSet[N] = {`
  - `114: double * xa1 = base;`
  - `115: double * xa2 = xa1 + 12;`
  - `118: // initialize segments touched by indexSet`
  - `121: base[i]=0.5*i;`
  - `124: #pragma omp parallel for // default static even scheduling may not trigger data race!`
  - `127: int idx = indexSet[i];`
  - `128: xa1[idx]+= 1.0;`
  - `129: xa2[idx]+= 3.0;`
  - `131: printf("x1[999]=%f xa2[1285]=%f\n", xa1[999], xa2[1285]);`

#### `DRB007-indirectaccess3-orig-yes`

- Index set: count=180, range=521..2013, pairs at distance 12: `521->533`
- Source context:
  - `51: indexSet[3]- indexSet[0] = 533 - 521 = 12`
  - `61: Data race pair: xa1[idx]@128:5:W vs. xa2[idx]@129:5:W`
  - `68: int indexSet[N] = {`
  - `114: double * xa1 = base;`
  - `115: double * xa2 = xa1 + 12;`
  - `118: // initialize segments touched by indexSet`
  - `121: base[i]=0.5*i;`
  - `124: #pragma omp parallel for // default static even scheduling may not trigger data race!`
  - `127: int idx = indexSet[i];`
  - `128: xa1[idx]+= 1.0;`
  - `129: xa2[idx]+= 3.0;`
  - `131: printf("x1[999]=%f xa2[1285]=%f\n", xa1[999], xa2[1285]);`

#### `DRB008-indirectaccess4-orig-yes`

- Index set: count=180, range=521..2013, pairs at distance 12: `521->533`
- Source context:
  - `47: Two pointers have a distance of 12 (xa2 - xa1 = 12).`
  - `51: indexSet[1]- indexSet[0] = 533 - 521 = 12`
  - `52: So xa1[idx] and xa2[idx] may cause loop carried dependence for N=0 and N=3.`
  - `62: Data race pair: xa1[idx]@128:5:W vs. xa2[idx]@129:5:W`
  - `69: int indexSet[N] = {`
  - `114: double * xa1 = base;`
  - `115: double * xa2 = xa1 + 12;`
  - `118: // initialize segments touched by indexSet`
  - `121: base[i]=0.5*i;`
  - `124: #pragma omp parallel for // default static even scheduling may not trigger data race!`
  - `127: int idx = indexSet[i];`
  - `128: xa1[idx]+= 1.0;`
  - `129: xa2[idx]+= 3.0;`
  - `131: printf("x1[999]=%f xa2[1285]=%f\n", xa1[999], xa2[1285]);`

#### `DRB044-adi-tile-no`

- Index set: no constant `indexSet` initializer found
- Source context:
  - `30: #pragma omp parallel for private(c4, c2, c3)`
  - `34: #pragma omp simd`
  - `36: X[c3][c4] = (((double )c3) * (c4 + 1) + 1) / n;`
  - `37: A[c3][c4] = (((double )c3) * (c4 + 2) + 2) / n;`
  - `38: B[c3][c4] = (((double )c3) * (c4 + 3) + 3) / n;`
  - `80: #pragma omp parallel for private(c15, c9, c8)`
  - `84: #pragma omp simd`
  - `86: B[c15][c9] = B[c15][c9] - A[c15][c9] * A[c15][c9] / B[c15][c9 - 1];`
  - `94: X[c15][c9] = X[c15][c9] - X[c15][c9 - 1] * A[c15][c9] / B[c15][c9 - 1];`
  - `102: X[c15][n - c9 - 2] = (X[c15][n - 2 - c9] - X[c15][n - 2 - c9 - 1] * A[c15][n - c9 - 3]) / B[c15][n - 3 - c9];`
  - `112: X[c15][n - 1] = X[c15][n - 1] / B[c15][n - 1];`
  - `122: B[c9][c15] = B[c9][c15] - A[c9][c15] * A[c9][c15] / B[c9 - 1][c15];`
  - `130: X[c9][c15] = X[c9][c15] - X[c9 - 1][c15] * A[c9][c15] / B[c9 - 1][c15];`
  - `138: X[n - 2 - c9][c15] = (X[n - 2 - c9][c15] - X[n - c9 - 3][c15] * A[n - 3 - c9][c15]) / B[n - 2 - c9][c15];`
  - `148: X[n - 1][c15] = X[n - 1][c15] / B[n - 1][c15];`

#### `DRB052-indirectaccesssharebase-orig-no`

- Index set: count=180, range=521..2013, pairs at distance 12: `none`
- Source context:
  - `60: int indexSet[N] = {`
  - `106: double * xa1 = base;`
  - `107: double * xa2 = base + 12;`
  - `112: base[i]=0.0;`
  - `115: #pragma omp parallel for`
  - `118: int idx = indexSet[i];`
  - `119: xa1[idx]+= 1.0;`
  - `120: xa2[idx]+= 3.0;`
  - `123: // verify the results, no overlapping of xa1 vs. xa2, no addition happens to the same element twice`


## CallGraphBench Missing Edges

Total edges: **13**

| Case | Caller | Callee | Diagnosis |
|---|---|---|---|
| c/c_dynamic | `c:c_dynamic:process_device` | `c:c_dynamic:tcp_open` | function_pointer_dispatch |
| c/c_dynamic | `c:c_dynamic:process_device` | `c:c_dynamic:tcp_read` | function_pointer_dispatch |
| c/c_dynamic | `c:c_dynamic:process_device` | `c:c_dynamic:udp_open` | function_pointer_dispatch |
| c/c_dynamic | `c:c_dynamic:process_device` | `c:c_dynamic:udp_read` | function_pointer_dispatch |
| c/c_multifile | `c:c_multifile:run_pipeline` | `c:c_multifile:suffix_handler` | cross_file_callback_registration |
| c/c_multifile | `c:c_multifile:run_pipeline` | `c:c_multifile:uppercase_handler` | cross_file_callback_registration |
| cpp/cpp_basic | `cpp:cpp_basic:User.welcome` | `cpp:cpp_basic:greet` | constructor_or_member_call |
| cpp/cpp_basic | `cpp:cpp_basic:main` | `cpp:cpp_basic:User.User` | constructor_or_member_call |
| cpp/cpp_basic | `cpp:cpp_basic:main` | `cpp:cpp_basic:User.welcome` | constructor_or_member_call |
| cpp/cpp_dispatch | `cpp:cpp_dispatch:execute_rule` | `cpp:cpp_dispatch:LengthRule.apply` | virtual_dispatch |
| cpp/cpp_dispatch | `cpp:cpp_dispatch:execute_rule` | `cpp:cpp_dispatch:PrefixRule.apply` | virtual_dispatch |
| cpp/cpp_multifile | `cpp:cpp_multifile:dispatch` | `cpp:cpp_multifile:JsonParser.handle` | virtual_dispatch |
| cpp/cpp_multifile | `cpp:cpp_multifile:dispatch` | `cpp:cpp_multifile:TextParser.handle` | virtual_dispatch |

### CallGraphBench Extracted Evidence

#### `c/c_dynamic`: `c:c_dynamic:process_device` -> `c:c_dynamic:tcp_open`

- Diagnosis: `function_pointer_dispatch`
- Gold kind/evidence: `indirect`; driver->open(target) can target tcp_open through tcp_driver.
- Caller source: `src/device.c:13`; `11: }`<br>`13: int process_device(const char *scheme, const char *target) {`<br>`14: struct Driver *driver = pick_driver(scheme);`<br>`15: int handle = driver->open(target);`
- Callee source: `src/drivers.c:3`; `1: #include "device.h"`<br>`3: int tcp_open(const char *target) {`<br>`4: (void)target;`<br>`5: return 7;`

#### `c/c_dynamic`: `c:c_dynamic:process_device` -> `c:c_dynamic:tcp_read`

- Diagnosis: `function_pointer_dispatch`
- Gold kind/evidence: `indirect`; driver->read(handle) can target tcp_read through tcp_driver.
- Caller source: `src/device.c:13`; `11: }`<br>`13: int process_device(const char *scheme, const char *target) {`<br>`14: struct Driver *driver = pick_driver(scheme);`<br>`15: int handle = driver->open(target);`
- Callee source: `src/drivers.c:13`; `11: }`<br>`13: int tcp_read(int handle) {`<br>`14: return handle + 100;`<br>`15: }`

#### `c/c_dynamic`: `c:c_dynamic:process_device` -> `c:c_dynamic:udp_open`

- Diagnosis: `function_pointer_dispatch`
- Gold kind/evidence: `indirect`; driver->open(target) can target udp_open through udp_driver.
- Caller source: `src/device.c:13`; `11: }`<br>`13: int process_device(const char *scheme, const char *target) {`<br>`14: struct Driver *driver = pick_driver(scheme);`<br>`15: int handle = driver->open(target);`
- Callee source: `src/drivers.c:8`; `6: }`<br>`8: int udp_open(const char *target) {`<br>`9: (void)target;`<br>`10: return 11;`

#### `c/c_dynamic`: `c:c_dynamic:process_device` -> `c:c_dynamic:udp_read`

- Diagnosis: `function_pointer_dispatch`
- Gold kind/evidence: `indirect`; driver->read(handle) can target udp_read through udp_driver.
- Caller source: `src/device.c:13`; `11: }`<br>`13: int process_device(const char *scheme, const char *target) {`<br>`14: struct Driver *driver = pick_driver(scheme);`<br>`15: int handle = driver->open(target);`
- Callee source: `src/drivers.c:17`; `15: }`<br>`17: int udp_read(int handle) {`<br>`18: return handle + 200;`<br>`19: }`

#### `c/c_multifile`: `c:c_multifile:run_pipeline` -> `c:c_multifile:suffix_handler`

- Diagnosis: `cross_file_callback_registration`
- Gold kind/evidence: `indirect`; handler(input) can target suffix_handler returned by select_handler.
- Caller source: `src/pipeline.c:12`; `10: }`<br>`12: char *run_pipeline(const char *input, int mode) {`<br>`13: StageHandler handler = select_handler(mode);`<br>`14: char *result = handler(input);`
- Callee source: `src/handlers.c:8`; `6: }`<br>`8: char *suffix_handler(const char *input) {`<br>`9: (void)input;`<br>`10: return "ada-ready";`

#### `c/c_multifile`: `c:c_multifile:run_pipeline` -> `c:c_multifile:uppercase_handler`

- Diagnosis: `cross_file_callback_registration`
- Gold kind/evidence: `indirect`; handler(input) can target uppercase_handler returned by select_handler.
- Caller source: `src/pipeline.c:12`; `10: }`<br>`12: char *run_pipeline(const char *input, int mode) {`<br>`13: StageHandler handler = select_handler(mode);`<br>`14: char *result = handler(input);`
- Callee source: `src/handlers.c:3`; `1: #include "pipeline.h"`<br>`3: char *uppercase_handler(const char *input) {`<br>`4: (void)input;`<br>`5: return "ADA";`

#### `cpp/cpp_basic`: `cpp:cpp_basic:User.welcome` -> `cpp:cpp_basic:greet`

- Diagnosis: `constructor_or_member_call`
- Gold kind/evidence: `direct`; welcome returns greet(name_).
- Caller source: `src/main.cpp:15`; `13: explicit User(std::string name) : name_(name) {}`<br>`15: std::string welcome() const {`<br>`16: return greet(name_);`<br>`17: }`
- Callee source: `src/main.cpp:7`; `5: }`<br>`7: std::string greet(const std::string &name) {`<br>`8: return formatName(name);`<br>`9: }`

#### `cpp/cpp_basic`: `cpp:cpp_basic:main` -> `cpp:cpp_basic:User.User`

- Diagnosis: `constructor_or_member_call`
- Gold kind/evidence: `constructor`; User user("ada") invokes the constructor.
- Caller source: `src/main.cpp:23`; `21: };`<br>`23: int main() {`<br>`24: User user("ada");`<br>`25: user.welcome();`
- Callee source: `src/main.cpp:13`; `11: class User {`<br>`12: public:`<br>`13: explicit User(std::string name) : name_(name) {}`<br>`15: std::string welcome() const {`

#### `cpp/cpp_basic`: `cpp:cpp_basic:main` -> `cpp:cpp_basic:User.welcome`

- Diagnosis: `constructor_or_member_call`
- Gold kind/evidence: `direct`; main calls user.welcome().
- Caller source: `src/main.cpp:23`; `21: };`<br>`23: int main() {`<br>`24: User user("ada");`<br>`25: user.welcome();`
- Callee source: `src/main.cpp:15`; `13: explicit User(std::string name) : name_(name) {}`<br>`15: std::string welcome() const {`<br>`16: return greet(name_);`<br>`17: }`

#### `cpp/cpp_dispatch`: `cpp:cpp_dispatch:execute_rule` -> `cpp:cpp_dispatch:LengthRule.apply`

- Diagnosis: `virtual_dispatch`
- Gold kind/evidence: `virtual`; rule.apply(value) can dispatch to LengthRule::apply.
- Caller source: `src/workflow.cpp:3`; `1: #include "rule.h"`<br>`3: bool execute_rule(const Rule& rule, const std::string& value) {`<br>`4: return rule.apply(value);`<br>`5: }`
- Callee source: `src/strategies.cpp:5`; `3: class LengthRule : public Rule {`<br>`4: public:`<br>`5: bool apply(const std::string& value) const override {`<br>`6: return value.size() > 3;`<br>`7: }`

#### `cpp/cpp_dispatch`: `cpp:cpp_dispatch:execute_rule` -> `cpp:cpp_dispatch:PrefixRule.apply`

- Diagnosis: `virtual_dispatch`
- Gold kind/evidence: `virtual`; rule.apply(value) can dispatch to PrefixRule::apply.
- Caller source: `src/workflow.cpp:3`; `1: #include "rule.h"`<br>`3: bool execute_rule(const Rule& rule, const std::string& value) {`<br>`4: return rule.apply(value);`<br>`5: }`
- Callee source: `src/strategies.cpp:12`; `10: class PrefixRule : public Rule {`<br>`11: public:`<br>`12: bool apply(const std::string& value) const override {`<br>`13: return value.rfind("ada", 0) == 0;`<br>`14: }`

#### `cpp/cpp_multifile`: `cpp:cpp_multifile:dispatch` -> `cpp:cpp_multifile:JsonParser.handle`

- Diagnosis: `virtual_dispatch`
- Gold kind/evidence: `virtual`; handler.handle(text) can dispatch to JsonParser::handle.
- Caller source: `src/router.cpp:3`; `1: #include "router.h"`<br>`3: std::string dispatch(Handler& handler, const std::string& text) {`<br>`4: return handler.handle(text);`<br>`5: }`
- Callee source: `src/handlers.cpp:5`; `3: class JsonParser : public Handler {`<br>`4: public:`<br>`5: std::string handle(const std::string& text) override {`<br>`6: return "json:" + text;`<br>`7: }`

#### `cpp/cpp_multifile`: `cpp:cpp_multifile:dispatch` -> `cpp:cpp_multifile:TextParser.handle`

- Diagnosis: `virtual_dispatch`
- Gold kind/evidence: `virtual`; handler.handle(text) can dispatch to TextParser::handle.
- Caller source: `src/router.cpp:3`; `1: #include "router.h"`<br>`3: std::string dispatch(Handler& handler, const std::string& text) {`<br>`4: return handler.handle(text);`<br>`5: }`
- Callee source: `src/handlers.cpp:12`; `10: class TextParser : public Handler {`<br>`11: public:`<br>`12: std::string handle(const std::string& text) override {`<br>`13: return "text:" + text;`<br>`14: }`


## MemSafety False Positives

Total safe tasks misclassified as leaks: **8**

| Case | Diagnosis | Evidence |
|---|---|---|
| 960521-1-2 | multi_branch_pointer_adjustment | 960521-1-2.i:536 a = malloc (n * sizeof(*a));<br>960521-1-2.i:536 a = malloc (n * sizeof(*a));<br>960521-1-2.i:537 b = malloc (n * sizeof(*b)); |
| lockfree-3_0 | linked_list_ownership_loop | lockfree-3.0.i:548 x1->next = t1;<br>lockfree-3.0.i:534 static struct cell *x1 = ((void *)0);<br>lockfree-3.0.i:533 static struct cell *t1 = ((void *)0); |
| test-0019-1 | struct_field_alias_and_early_return | test-0019-1.i:530 pdata->hi = malloc(24);<br>test-0019-1.i:534 void *lo = data.lo;<br>test-0019-1.i:538 free(lo); |
| test-0102-1 | linked_list_field_alias | test-0102-1.i:555 now = next;<br>test-0102-1.i:587 struct top_list *top = malloc(sizeof(*top));<br>test-0102-1.i:571 struct sub_list *sub = malloc(sizeof(*sub)); |
| test-0134 | container_of_pointer_arithmetic | test-0134.i:567 struct node *node = malloc(sizeof *node);<br>test-0134.i:585 free(((struct node *)((char *)(next)-(unsigned long)(&((struct node *)0)->linkage))));<br>test-0134.i:567 struct node *node = malloc(sizeof *node); |
| test-0214 | linked_list_branch_ownership | test-0214.i:709 item_p item = (*list)[from];<br>test-0214.i:741 item_p cursor = list[end_point];<br>test-0214.i:745 cursor = next; |
| test-0217 | linked_list_branch_ownership | test-0217.i:667 user_item_p item = malloc(sizeof *item);<br>test-0217.i:749 cursor = next;<br>test-0217.i:717 free(item); |
| test-0219 | nested_struct_and_custom_calloc | test-0219.i:670 list = next;<br>test-0219.i:695 list = next;<br>test-0219.i:669 free(list); |

### MemSafety Extracted Evidence

#### `960521-1-2`

- Task: `c/memsafety/960521-1-2.yml`; inputs: `960521-1-2.i`
- Source context:
  - `535: n = 32768;`
  - `536: a = malloc (n * sizeof(*a));`
  - `537: b = malloc (n * sizeof(*b));`
  - `538: *b++ = 0;`
  - `542: else`
  - `543: { free(a); free(b-1); }`
  - `544: return 0;`

#### `lockfree-3_0`

- Task: `c/memsafety/lockfree-3.0.yml`; inputs: `lockfree-3.0.i`
- Source context:
  - `547: case 4:`
  - `548: x1->next = t1;`
  - `549: return;`
  - `533: static struct cell *t1 = ((void *)0);`
  - `534: static struct cell *x1 = ((void *)0);`
  - `535: switch (pc1++) {`
  - `532: {`
  - `600: while (garbage) {`
  - `601: struct cell *next = garbage->next;`
  - `602: free(garbage);`
  - `536: case 1:`
  - `537: x1 = malloc(sizeof(*x1));`
  - `538: x1->data = 0;`

#### `test-0019-1`

- Task: `c/memsafety/test-0019-1.yml`; inputs: `test-0019-1.i`
- Source context:
  - `529: pdata->lo = malloc(16);`
  - `530: pdata->hi = malloc(24);`
  - `531: }`
  - `533: {`
  - `534: void *lo = data.lo;`
  - `535: void *hi = data.hi;`
  - `537: return;`
  - `538: free(lo);`
  - `539: free(hi);`
  - `528: {`

#### `test-0102-1`

- Task: `c/memsafety/test-0102-1.yml`; inputs: `test-0102-1.i`
- Source context:
  - `554: free(now);`
  - `555: now = next;`
  - `556: }`
  - `586: {`
  - `587: struct top_list *top = malloc(sizeof(*top));`
  - `588: if (!top)`
  - `570: {`
  - `571: struct sub_list *sub = malloc(sizeof(*sub));`
  - `572: if (!sub)`
  - `550: {`
  - `551: struct sub_list *now = ((struct sub_list *)((char *)(head->next)-(unsigned long)(&((struct sub_list *)0)->link)));`
  - `552: while (&now->link != (head)) {`

#### `test-0134`

- Task: `c/memsafety/test-0134.yml`; inputs: `test-0134.i`
- Source context:
  - `566: {`
  - `567: struct node *node = malloc(sizeof *node);`
  - `568: if (!node)`
  - `584: gl_list.next = next->next;`
  - `585: free(((struct node *)((char *)(next)-(unsigned long)(&((struct node *)0)->linkage))));`
  - `586: }`

#### `test-0214`

- Task: `c/memsafety/test-0214.yml`; inputs: `test-0214.i`
- Source context:
  - `708: const direction_t term_field = (LIST_END == from) ? ITEM_NEXT : ITEM_PREV;`
  - `709: item_p item = (*list)[from];`
  - `710: item_p next = (*item)[next_field];`
  - `740: }`
  - `741: item_p cursor = list[end_point];`
  - `742: while (cursor) {`
  - `744: free(cursor);`
  - `745: cursor = next;`
  - `746: }`
  - `743: item_p next = (*cursor)[direction];`
  - `661: if (no_beg != no_end)`
  - `662: free(list);`
  - `663: return no_beg;`

#### `test-0217`

- Task: `c/memsafety/test-0217.yml`; inputs: `test-0217.i`
- Source context:
  - `666: {`
  - `667: user_item_p item = malloc(sizeof *item);`
  - `668: if (!item)`
  - `748: free(cursor);`
  - `749: cursor = next;`
  - `750: }`
  - `716: (*list)[from] = next;`
  - `717: free(item);`
  - `718: }`
  - `661: if (no_beg != no_end)`
  - `662: free(list);`
  - `663: return no_beg;`
  - `712: const direction_t term_field = (LIST_END == from) ? ITEM_NEXT : ITEM_PREV;`
  - `713: item_p item = (*list)[from];`
  - `714: item_p next = (*item)[next_field];`

#### `test-0219`

- Task: `c/memsafety/test-0219.yml`; inputs: `test-0219.i`
- Source context:
  - `669: free(list);`
  - `670: list = next;`
  - `671: }`
  - `694: free(list);`
  - `695: list = next;`
  - `696: }`
  - `668: free(list->proto);`
  - `693: l2_destroy(list->l2);`
  - `657: abort();`
  - `658: item->proto = malloc(119U);`
  - `659: if (!item->proto)`


## Improvement Actions

- CG-Bench: add interprocedural callback argument mapping, struct field-index resolution, table initializer parsing, cast evaluation and virtual dispatch.
- DataRaceBench: add constant index-set analysis, pointer-offset alias tracking and affine loop dependence checks.
- CallGraphBench: add function-pointer dispatch resolution, cross-file callback registration, C++ constructor/member and virtual-dispatch edges.
- MemSafety: add branch-sensitive resource pairing, pointer arithmetic/container_of normalization, field aliases and ownership transfer.
- Re-run the same frozen v1 cases first; publish any sample or Gold change only as external-v2.
