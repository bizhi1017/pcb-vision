/* Standalone CPU-only ABI/CRC/reconstruction check under aarch64 QEMU. */
#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <string.h>
int fpga_decode_profile(const void*,size_t,unsigned,void*,size_t,unsigned,unsigned);
static unsigned char *load(const char *name,size_t *n){
 FILE *f=fopen(name,"rb");if(!f)return NULL;
 if(fseek(f,0,SEEK_END)){fclose(f);return NULL;}
 long size=ftell(f);if(size<=0){fclose(f);return NULL;}
 *n=(size_t)size;rewind(f);unsigned char *p=malloc(*n);
 if(!p||fread(p,1,*n,f)!=*n){free(p);fclose(f);return NULL;}
 fclose(f);return p;
}
int main(int argc,char **argv){
 if(argc!=7)return 2;
 size_t n=0,m=0;unsigned char *wire=load(argv[1],&n),*want=load(argv[2],&m);
 if(!wire||!want)return 3;
 unsigned camera=(unsigned)strtoul(argv[3],NULL,10),raw=(unsigned)strtoul(argv[4],NULL,10),gray=(unsigned)strtoul(argv[5],NULL,10);
 unsigned char *out=malloc(m+16);if(!out)return 4;
 int got=fpga_decode_profile(wire,n,camera,out,m+16,raw,gray);
 if(got!=(int)m+16||memcmp(out,want,m)){fprintf(stderr,"decode/bytes failed: %d\n",got);return 5;}
 if(fpga_decode_profile(wire,n,camera^1u,out,m+16,raw,gray)>=0)return 6;
 wire[n/2]^=1;if(fpga_decode_profile(wire,n,camera,out,m+16,raw,gray)>=0)return 7;
 printf("ARM CPU PASS: %s, source bytes %zu, packet bytes %zu\n",argv[6],m,n);
 free(wire);free(want);free(out);return 0;
}
